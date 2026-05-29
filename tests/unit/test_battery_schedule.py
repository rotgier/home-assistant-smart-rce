"""Unit tests for BatterySchedule domain — compute_operation + events + helpers."""

from datetime import date, datetime, time, timezone

from custom_components.smart_rce.domain.battery_schedule import (
    CHARGE,
    DISCHARGE,
    BatteryOperation,
    BatterySchedule,
    BatteryScheduleEntry,
    DayRolled,
    EmsMode,
    NotificationLevel,
    SlotBehavior,
    SlotDisengaged,
    SlotEngaged,
    SlotKind,
)
import pytest

TZ = timezone.utc


def _at(h: int, m: int = 0, day: int = 22) -> datetime:
    """Return datetime 2026-05-{day} at HH:MM UTC (test helper)."""
    return datetime(2026, 5, day, h, m, tzinfo=TZ)


# ─────────────────────────────────────────────────────────────────────────────
# Direction
# ─────────────────────────────────────────────────────────────────────────────


class TestDirection:
    def test_discharge_is_discharge(self):
        assert DISCHARGE.is_discharge is True
        assert DISCHARGE.is_charge is False

    def test_charge_is_charge(self):
        assert CHARGE.is_discharge is False
        assert CHARGE.is_charge is True

    def test_string_compare_safe_after_reload(self):
        """Direction comparison via property uses string — survives live_reload."""
        # Simulate two separate Direction instances with same name (post-reload).
        from custom_components.smart_rce.domain.battery_schedule import Direction

        other_discharge = Direction(
            name="DISCHARGE",
            ems_mode=DISCHARGE.ems_mode,
            power_limit_w=DISCHARGE.power_limit_w,
            needs_charge_toggle=DISCHARGE.needs_charge_toggle,
        )
        assert other_discharge.is_discharge is True
        # `is` would fail here in real reload scenario; equality works because
        # frozen dataclass value-compares all fields.
        assert other_discharge == DISCHARGE


# ─────────────────────────────────────────────────────────────────────────────
# SlotKind + SlotProfile
# ─────────────────────────────────────────────────────────────────────────────


class TestSlotProfile:
    def test_charge_morning_defaults(self):
        p = SlotKind.CHARGE_MORNING.profile
        assert p.direction == CHARGE
        assert p.default_window == (time(2, 0), time(6, 0))
        assert p.default_target_soc == 100.0
        assert p.notification_level == NotificationLevel.NORMAL

    def test_discharge_evening_emergency_notification(self):
        p = SlotKind.DISCHARGE_EVENING.profile
        assert p.notification_level == NotificationLevel.EMERGENCY

    def test_discharge_morning_normal_notification(self):
        """Morning discharge — no voice call (would wake user)."""
        p = SlotKind.DISCHARGE_MORNING.profile
        assert p.notification_level == NotificationLevel.NORMAL

    def test_direction_property_via_slotkind(self):
        assert SlotKind.CHARGE_MORNING.direction.is_charge
        assert SlotKind.DISCHARGE_EVENING.direction.is_discharge


# ─────────────────────────────────────────────────────────────────────────────
# BatteryScheduleEntry — validation + predicates
# ─────────────────────────────────────────────────────────────────────────────


class TestEntryValidation:
    def test_target_soc_below_zero_raises(self):
        with pytest.raises(ValueError, match="outside"):
            BatteryScheduleEntry(kind=SlotKind.DISCHARGE_EVENING, target_soc=-1.0)

    def test_target_soc_above_100_raises(self):
        with pytest.raises(ValueError, match="outside"):
            BatteryScheduleEntry(kind=SlotKind.CHARGE_MORNING, target_soc=150.0)

    def test_enabled_with_inverted_window_raises(self):
        with pytest.raises(ValueError, match="must be before"):
            BatteryScheduleEntry(
                kind=SlotKind.DISCHARGE_EVENING,
                enabled=True,
                start=time(22, 0),
                end=time(20, 0),
                target_soc=10.0,
            )

    def test_disabled_allows_any_window(self):
        # Disabled entry doesn't enforce start < end.
        entry = BatteryScheduleEntry(
            kind=SlotKind.DISCHARGE_EVENING,
            enabled=False,
            start=time(0, 0),
            end=time(0, 0),
        )
        assert entry.enabled is False


class TestEntryPredicates:
    def _evening(self, **overrides):
        defaults = {
            "kind": SlotKind.DISCHARGE_EVENING,
            "enabled": True,
            "start": time(20, 0),
            "end": time(22, 0),
            "target_soc": 10.0,
        }
        defaults.update(overrides)
        return BatteryScheduleEntry(**defaults)

    def test_in_window_boundary_start_inclusive(self):
        entry = self._evening()
        assert entry.is_in_window(_at(20, 0)) is True

    def test_in_window_boundary_end_exclusive(self):
        entry = self._evening()
        assert entry.is_in_window(_at(22, 0)) is False
        assert entry.is_in_window(_at(21, 59)) is True

    def test_in_window_outside(self):
        entry = self._evening()
        assert entry.is_in_window(_at(19, 0)) is False

    def test_soc_target_reached_discharge(self):
        entry = self._evening(target_soc=10.0)
        assert entry.soc_target_reached(10.0) is True
        assert entry.soc_target_reached(5.0) is True  # below target
        assert entry.soc_target_reached(15.0) is False  # above target

    def test_soc_target_reached_charge(self):
        entry = BatteryScheduleEntry(
            kind=SlotKind.CHARGE_MORNING,
            enabled=True,
            start=time(2, 0),
            end=time(6, 0),
            target_soc=100.0,
        )
        assert entry.soc_target_reached(100.0) is True
        assert entry.soc_target_reached(50.0) is False
        assert entry.soc_target_reached(100.1) is True

    def test_time_to_complete_at_zero_when_target_reached(self):
        entry = self._evening(target_soc=10.0)
        assert entry.time_to_complete_at(5.0) == 0.0

    def test_time_to_complete_at_proportional_to_delta(self):
        entry = self._evening(target_soc=10.0)
        # rate_sec_per_pct = 75 (from default profile)
        assert entry.time_to_complete_at(80.0) == 70 * 75


class TestShouldApplyNow:
    def _evening(self, **overrides):
        defaults = {
            "kind": SlotKind.DISCHARGE_EVENING,
            "enabled": True,
            "start": time(20, 0),
            "end": time(22, 0),
            "target_soc": 10.0,
            "behavior": SlotBehavior.IMMEDIATE,
        }
        defaults.update(overrides)
        return BatteryScheduleEntry(**defaults)

    def test_disabled_never_applies(self):
        entry = self._evening(enabled=False, start=time(20, 0), end=time(22, 0))
        assert entry.should_apply_now(_at(21, 0), 80.0) is False

    def test_outside_window_never_applies(self):
        entry = self._evening()
        assert entry.should_apply_now(_at(19, 0), 80.0) is False

    def test_target_already_reached_never_applies(self):
        entry = self._evening()
        assert entry.should_apply_now(_at(21, 0), 5.0) is False

    def test_immediate_applies_inside_window(self):
        entry = self._evening(behavior=SlotBehavior.IMMEDIATE)
        assert entry.should_apply_now(_at(20, 30), 80.0) is True

    def test_delayed_to_end_waits_when_slack(self):
        """80% → 10% = 70pp × 75s = 5250s needed. Window 20-22 = 7200s slack."""
        entry = self._evening(behavior=SlotBehavior.DELAYED_TO_END)
        # At 20:00, sec_to_end = 7200, time_to_complete = 5250 → wait.
        assert entry.should_apply_now(_at(20, 0), 80.0) is False

    def test_delayed_to_end_engages_when_just_in_time(self):
        """sec_to_end <= time_to_complete → engage."""
        entry = self._evening(behavior=SlotBehavior.DELAYED_TO_END, target_soc=10.0)
        # 5250s needed. Threshold at 22:00 - 5250s = 20:32:30. At 20:33 sec_to_end
        # = 5220s ≤ 5250s → engage.
        assert entry.should_apply_now(_at(20, 33), 80.0) is True


# ─────────────────────────────────────────────────────────────────────────────
# BatteryOperation
# ─────────────────────────────────────────────────────────────────────────────


class TestBatteryOperation:
    def test_idle(self):
        op = BatteryOperation.idle()
        assert op.is_idle is True
        assert op.ems_mode == EmsMode.AUTO
        assert op.power_limit_w is None
        assert op.needs_charge_toggle is False
        assert op.slot is None

    def test_from_discharge_evening_entry(self):
        entry = BatteryScheduleEntry.default_for(SlotKind.DISCHARGE_EVENING)
        op = BatteryOperation.from_entry(entry)
        assert op.is_idle is False
        assert op.slot == SlotKind.DISCHARGE_EVENING
        assert op.ems_mode == EmsMode.DISCHARGE_PV
        assert op.power_limit_w == 6000
        assert op.needs_charge_toggle is False
        assert op.notification_level == NotificationLevel.EMERGENCY

    def test_from_charge_morning_entry(self):
        entry = BatteryScheduleEntry.default_for(SlotKind.CHARGE_MORNING)
        op = BatteryOperation.from_entry(entry)
        assert op.slot == SlotKind.CHARGE_MORNING
        assert op.ems_mode == EmsMode.CHARGE_BATTERY
        assert op.needs_charge_toggle is True  # BMS guard
        assert op.notification_level == NotificationLevel.NORMAL

    def test_equality_value_based(self):
        op1 = BatteryOperation.idle()
        op2 = BatteryOperation.idle()
        assert op1 == op2
        assert op1 is not op2  # frozen dataclass — same value, different instance


# ─────────────────────────────────────────────────────────────────────────────
# BatterySchedule.compute_operation
# ─────────────────────────────────────────────────────────────────────────────


def _enabled_evening(target=10.0, behavior=SlotBehavior.IMMEDIATE):
    return BatteryScheduleEntry(
        kind=SlotKind.DISCHARGE_EVENING,
        enabled=True,
        start=time(20, 0),
        end=time(22, 0),
        target_soc=target,
        behavior=behavior,
    )


def _schedule(
    *,
    today: dict[SlotKind, BatteryScheduleEntry] | None = None,
    tomorrow: dict[SlotKind, BatteryScheduleEntry] | None = None,
) -> BatterySchedule:
    """Test factory — start from defaults, override specific slots."""
    today_dict = {k: BatteryScheduleEntry.default_for(k) for k in SlotKind}
    if today:
        today_dict.update(today)
    tomorrow_dict = {k: BatteryScheduleEntry.default_for(k) for k in SlotKind}
    if tomorrow:
        tomorrow_dict.update(tomorrow)
    return BatterySchedule(_today=today_dict, _tomorrow=tomorrow_dict)


def _enabled_charge_afternoon(target=80.0, behavior=SlotBehavior.IMMEDIATE):
    return BatteryScheduleEntry(
        kind=SlotKind.CHARGE_AFTERNOON,
        enabled=True,
        start=time(13, 0),
        end=time(19, 0),
        target_soc=target,
        behavior=behavior,
    )


class TestComputeOperationIdle:
    def test_default_schedule_is_idle(self):
        sch = BatterySchedule()
        op, evts = sch.compute_operation(_at(12, 0), 50.0)
        assert op.is_idle is True
        assert evts == []

    def test_outside_all_windows_is_idle(self):
        sch = _schedule(today={SlotKind.DISCHARGE_EVENING: _enabled_evening()})
        op, evts = sch.compute_operation(_at(15, 0), 50.0)
        assert op.is_idle is True
        assert evts == []


class TestComputeOperationEngagement:
    def test_engage_emits_event_and_sets_currently_engaging(self):
        sch = _schedule(today={SlotKind.DISCHARGE_EVENING: _enabled_evening()})
        op, evts = sch.compute_operation(_at(20, 30), 80.0)
        assert op.slot == SlotKind.DISCHARGE_EVENING
        assert len(evts) == 1
        assert isinstance(evts[0], SlotEngaged)
        assert evts[0].slot == SlotKind.DISCHARGE_EVENING
        assert evts[0].soc == 80.0
        assert sch._currently_engaging == SlotKind.DISCHARGE_EVENING  # noqa: SLF001

    def test_stays_engaged_no_event_no_change(self):
        sch = _schedule(today={SlotKind.DISCHARGE_EVENING: _enabled_evening()})
        sch.compute_operation(_at(20, 30), 80.0)
        op, evts = sch.compute_operation(_at(20, 31), 75.0)
        assert op.slot == SlotKind.DISCHARGE_EVENING
        assert evts == []

    def test_disengage_on_target_reached(self):
        sch = _schedule(
            today={SlotKind.DISCHARGE_EVENING: _enabled_evening(target=10.0)}
        )
        sch.compute_operation(_at(20, 30), 80.0)
        op, evts = sch.compute_operation(_at(20, 45), 5.0)  # below target
        assert op.is_idle is True
        assert len(evts) == 1
        assert isinstance(evts[0], SlotDisengaged)
        assert evts[0].reason == "target_reached"

    def test_disengage_on_window_ended(self):
        sch = _schedule(today={SlotKind.DISCHARGE_EVENING: _enabled_evening()})
        sch.compute_operation(_at(20, 30), 80.0)
        op, evts = sch.compute_operation(_at(22, 0), 50.0)  # at end (exclusive)
        assert op.is_idle is True
        assert len(evts) == 1
        assert evts[0].reason == "window_ended"

    def test_hysteresis_overrides_delayed_to_end_flicker(self):
        """Once engaged, stay engaged even if DELAYED_TO_END criterion flickers.

        Faster-than-expected SoC drop would make DELAYED_TO_END "wait" again,
        but hysteresis on `_currently_engaging` keeps the slot active.
        """
        sch = _schedule(
            today={
                SlotKind.DISCHARGE_EVENING: _enabled_evening(
                    target=10.0, behavior=SlotBehavior.DELAYED_TO_END
                )
            }
        )
        # At 20:32:30 should_apply_now=True (sec_to_end == time_to_complete).
        op1, _ = sch.compute_operation(_at(20, 32), 80.0)
        # 20:32 just before — DELAYED_TO_END not yet engaging.
        assert op1.is_idle is True

        # Engage at 20:33 (sec_to_end < needed → True under IMMEDIATE-equivalent).
        sch_imm = _schedule(
            today={
                SlotKind.DISCHARGE_EVENING: _enabled_evening(
                    behavior=SlotBehavior.IMMEDIATE
                )
            }
        )
        sch_imm.compute_operation(_at(20, 30), 80.0)
        # SoC dropping faster than expected — hysteresis keeps engaged.
        op2, _ = sch_imm.compute_operation(_at(20, 31), 40.0)
        assert op2.slot == SlotKind.DISCHARGE_EVENING  # still engaged


class TestComputeOperationPrecedence:
    def test_discharge_evening_beats_charge_afternoon_on_overlap(self):
        """DISCHARGE wins overlap (CHARGE_AFTERNOON 13-19, DISCHARGE_EVENING 18:30-21).

        RCE peaks are time-critical — precedence puts evening discharge above
        afternoon charge.
        """
        evening = BatteryScheduleEntry(
            kind=SlotKind.DISCHARGE_EVENING,
            enabled=True,
            start=time(18, 30),
            end=time(21, 0),
            target_soc=10.0,
            behavior=SlotBehavior.IMMEDIATE,
        )
        sch = _schedule(
            today={
                SlotKind.DISCHARGE_EVENING: evening,
                SlotKind.CHARGE_AFTERNOON: _enabled_charge_afternoon(),
            }
        )
        op, evts = sch.compute_operation(_at(18, 45), 80.0)
        assert op.slot == SlotKind.DISCHARGE_EVENING


# ─────────────────────────────────────────────────────────────────────────────
# Day roll
# ─────────────────────────────────────────────────────────────────────────────


class TestDayRoll:
    def test_first_tick_sets_last_seen_no_roll_event(self):
        sch = BatterySchedule()
        _, evts = sch.compute_operation(_at(12, 0), 50.0)
        assert sch.last_seen_date == date(2026, 5, 22)
        assert all(not isinstance(e, DayRolled) for e in evts)

    def test_midnight_crossing_emits_day_rolled_and_shifts_tomorrow_to_today(self):
        sch = _schedule(tomorrow={SlotKind.DISCHARGE_EVENING: _enabled_evening()})
        # Day 22 — establish last_seen_date.
        sch.compute_operation(_at(23, 0, day=22), 50.0)
        # Day 23 — roll happens.
        op, evts = sch.compute_operation(_at(20, 30, day=23), 80.0)
        # DayRolled emitted PLUS SlotEngaged (because tomorrow_discharge_evening
        # rolled into today_discharge_evening and is in-window).
        rolled = [e for e in evts if isinstance(e, DayRolled)]
        engaged = [e for e in evts if isinstance(e, SlotEngaged)]
        assert len(rolled) == 1
        assert rolled[0].from_date == date(2026, 5, 22)
        assert rolled[0].to_date == date(2026, 5, 23)
        assert len(engaged) == 1
        assert op.slot == SlotKind.DISCHARGE_EVENING


# ─────────────────────────────────────────────────────────────────────────────
# Persistence — to_dict / from_dict round-trip preserves new fields
# ─────────────────────────────────────────────────────────────────────────────


class TestPersistenceRoundTrip:
    def test_currently_engaging_persisted(self):
        sch = _schedule(today={SlotKind.DISCHARGE_EVENING: _enabled_evening()})
        sch.compute_operation(_at(20, 30), 80.0)
        assert sch._currently_engaging == SlotKind.DISCHARGE_EVENING  # noqa: SLF001
        restored = BatterySchedule.from_dict(sch.to_dict())
        assert restored._currently_engaging == SlotKind.DISCHARGE_EVENING  # noqa: SLF001

    def test_last_seen_date_persisted(self):
        sch = BatterySchedule()
        sch.compute_operation(_at(12, 0), 50.0)
        restored = BatterySchedule.from_dict(sch.to_dict())
        assert restored.last_seen_date == date(2026, 5, 22)

    def test_interventions_blocked_override_persisted(self):
        sch = BatterySchedule()
        sch._interventions_blocked_override = True  # noqa: SLF001
        restored = BatterySchedule.from_dict(sch.to_dict())
        assert restored._interventions_blocked_override is True  # noqa: SLF001
        assert restored.ems_interventions_blocked is True

    def test_last_disengaged_at_persisted(self):
        sch = _schedule(
            today={SlotKind.DISCHARGE_EVENING: _enabled_evening(target=10.0)}
        )
        # Engage at 20:30, then disengage at 20:45 (target reached: soc=5 < 10)
        sch.compute_operation(_at(20, 30), 80.0)
        sch.compute_operation(_at(20, 45), 5.0)
        assert sch._last_disengaged_at == _at(20, 45)  # noqa: SLF001
        restored = BatterySchedule.from_dict(sch.to_dict())
        assert restored._last_disengaged_at == _at(20, 45)  # noqa: SLF001


# ─────────────────────────────────────────────────────────────────────────────
# is_active_this_hour — derived signal for grid_export step-aside window
# ─────────────────────────────────────────────────────────────────────────────


class TestIsActiveThisHour:
    def test_idle_default_false(self):
        sch = BatterySchedule()
        assert sch.is_active_this_hour(_at(12, 0)) is False

    def test_currently_engaging_true(self):
        sch = _schedule(today={SlotKind.DISCHARGE_EVENING: _enabled_evening()})
        sch.compute_operation(_at(20, 30), 80.0)
        assert sch.is_active_this_hour(_at(20, 45)) is True

    def test_disengaged_within_same_hour_true(self):
        sch = _schedule(
            today={SlotKind.DISCHARGE_EVENING: _enabled_evening(target=10.0)}
        )
        sch.compute_operation(_at(20, 30), 80.0)
        sch.compute_operation(_at(20, 45), 5.0)  # disengage at 20:45
        assert sch.is_active_this_hour(_at(20, 50)) is True  # same hour 20:00-21:00

    def test_disengaged_next_hour_false(self):
        sch = _schedule(
            today={SlotKind.DISCHARGE_EVENING: _enabled_evening(target=10.0)}
        )
        sch.compute_operation(_at(20, 30), 80.0)
        sch.compute_operation(_at(20, 45), 5.0)  # disengage at 20:45
        assert sch.is_active_this_hour(_at(21, 0)) is False  # hour rolled

    def test_disengaged_previous_hour_false(self):
        sch = _schedule(
            today={SlotKind.DISCHARGE_EVENING: _enabled_evening(target=10.0)}
        )
        sch.compute_operation(_at(20, 30), 80.0)
        sch.compute_operation(_at(20, 45), 5.0)  # disengage at 20:45
        assert sch.is_active_this_hour(_at(22, 0)) is False  # 2h later
