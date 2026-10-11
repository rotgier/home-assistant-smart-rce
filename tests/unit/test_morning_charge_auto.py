"""CHARGE_MORNING_AUTO — forecast-driven morning charge before T1."""

from datetime import date, datetime, time, timezone
from types import SimpleNamespace

from custom_components.smart_rce.application.battery_schedule_service import (
    BatteryScheduleService,
)
from custom_components.smart_rce.domain.battery_schedule import (
    BatterySchedule,
    BatteryScheduleEntry,
    DisengageReason,
    MorningChargeTarget,
    SlotBehavior,
    SlotDisengaged,
    SlotEngaged,
    SlotKind,
)
from custom_components.smart_rce.domain.pv_forecast import PvForecast
from custom_components.smart_rce.domain.target_soc import TargetSoc, TargetSocResult

TZ = timezone.utc
DAY = date(2026, 10, 12)  # Monday


def _at(h: int, m: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


def _auto(fallback: float = 60.0, *, enabled: bool = True) -> BatteryScheduleEntry:
    return BatteryScheduleEntry(
        kind=SlotKind.CHARGE_MORNING_AUTO,
        enabled=enabled,
        start=time(4, 0),
        end=time(7, 0),
        target_soc=fallback,
        behavior=SlotBehavior.DELAYED_TO_END,
    )


def _target(value: int, day: date = DAY, source: str = "live") -> MorningChargeTarget:
    return MorningChargeTarget(
        day=day, value=value, flat=value, prev_without_worst=None, source=source
    )


def _schedule(*entries: BatteryScheduleEntry) -> BatterySchedule:
    today = BatteryScheduleEntry.defaults_for_all_kinds()
    for e in entries:
        today[e.kind] = e
    return BatterySchedule(today=today)


def _result(value: int) -> TargetSocResult:
    return TargetSocResult(value=value, buckets=[], dip_kwh=0.0)


def _engaged(events) -> SlotEngaged:
    return next(e for e in events if isinstance(e, SlotEngaged))


class TestPrevWithoutWorst:
    def test_drops_only_the_single_highest(self):
        assert MorningChargeTarget.prev_without_worst_of([24, 29, 28, 30, 23]) == 29

    def test_tie_at_the_top_keeps_the_twin(self):
        assert MorningChargeTarget.prev_without_worst_of([30, 30, 20]) == 30

    def test_single_value_is_the_answer(self):
        assert MorningChargeTarget.prev_without_worst_of([41]) == 41

    def test_empty_is_none(self):
        assert MorningChargeTarget.prev_without_worst_of([]) is None


class TestFromTargetSoc:
    def test_max_of_flat_and_prev_without_worst(self):
        ts = TargetSoc(
            variant=PvForecast.LIVE,
            flat=_result(25),
            prev_days=[_result(v) for v in (24, 29, 28, 30, 23)] + [None],
        )
        target = MorningChargeTarget.from_target_soc(DAY, ts, source="live")
        assert target is not None
        assert (target.value, target.flat, target.prev_without_worst) == (29, 25, 29)

    def test_flat_wins_when_higher(self):
        ts = TargetSoc(
            variant=PvForecast.LIVE, flat=_result(36), prev_days=[_result(30)]
        )
        target = MorningChargeTarget.from_target_soc(DAY, ts, source="live")
        assert target is not None and target.value == 36

    def test_capped_at_100(self):
        ts = TargetSoc(variant=PvForecast.LIVE, flat=_result(126), prev_days=[])
        target = MorningChargeTarget.from_target_soc(DAY, ts, source="live")
        assert target is not None and target.value == 100

    def test_nothing_computed_is_none(self):
        ts = TargetSoc(variant=PvForecast.LIVE, flat=None, prev_days=[None, None])
        assert MorningChargeTarget.from_target_soc(DAY, ts, source="live") is None


class TestEffectiveTarget:
    def test_engages_with_forecast_target_and_reports_source(self):
        sch = _schedule(_auto())
        sch.set_morning_target(_target(40))
        _, events = sch.compute_operation(_at(6, 40), 20.0, is_workday=True)
        engaged = _engaged(events)
        assert engaged.slot is SlotKind.CHARGE_MORNING_AUTO
        assert (engaged.target_soc, engaged.target_source) == (40.0, "live")

    def test_without_target_falls_back_to_slot_number(self):
        sch = _schedule(_auto(fallback=60.0))
        # 20 -> 60% = 3000 s + 240 s lead -> starts by 06:06.
        _, events = sch.compute_operation(_at(6, 10), 20.0, is_workday=True)
        engaged = _engaged(events)
        assert (engaged.target_soc, engaged.target_source) == (60.0, "fallback")

    def test_target_for_another_day_is_ignored(self):
        sch = _schedule(_auto(fallback=60.0))
        sch.set_morning_target(_target(40, day=date(2026, 10, 9)))
        _, events = sch.compute_operation(_at(6, 10), 20.0, is_workday=True)
        assert _engaged(events).target_source == "fallback"

    def test_slot_number_untouched_by_target(self):
        sch = _schedule(_auto(fallback=60.0))
        sch.set_morning_target(_target(40))
        sch.compute_operation(_at(6, 40), 20.0, is_workday=True)
        assert sch.today_entry_for(SlotKind.CHARGE_MORNING_AUTO).target_soc == 60.0


class TestDelayedLead:
    """20 -> 40% = 20 pp x 75 s = 1500 s; lead adds 240 s -> start by 06:31."""

    def test_waits_while_more_than_time_plus_lead_remains(self):
        sch = _schedule(_auto())
        sch.set_morning_target(_target(40))
        _, events = sch.compute_operation(_at(6, 30), 20.0, is_workday=True)
        assert not any(isinstance(e, SlotEngaged) for e in events)

    def test_starts_four_minutes_before_the_rate_model_would(self):
        sch = _schedule(_auto())
        sch.set_morning_target(_target(40))
        # 06:32 leaves 1680 s: above the bare 1500 s, inside 1500 + 240.
        _, events = sch.compute_operation(_at(6, 32), 20.0, is_workday=True)
        assert _engaged(events).slot is SlotKind.CHARGE_MORNING_AUTO

    def test_other_slots_have_no_lead(self):
        manual = BatteryScheduleEntry(
            kind=SlotKind.CHARGE_MORNING,
            enabled=True,
            start=time(4, 0),
            end=time(7, 0),
            target_soc=40.0,
            behavior=SlotBehavior.DELAYED_TO_END,
        )
        sch = _schedule(manual)
        _, events = sch.compute_operation(_at(6, 32), 20.0, is_workday=True)
        assert not any(isinstance(e, SlotEngaged) for e in events)


class TestStartGap:
    def test_two_points_short_does_not_start(self):
        sch = _schedule(_auto())
        sch.set_morning_target(_target(40))
        _, events = sch.compute_operation(_at(6, 59), 38.0, is_workday=True)
        assert not any(isinstance(e, SlotEngaged) for e in events)

    def test_three_points_short_starts(self):
        sch = _schedule(_auto())
        sch.set_morning_target(_target(40))
        _, events = sch.compute_operation(_at(6, 59), 37.0, is_workday=True)
        assert _engaged(events).slot is SlotKind.CHARGE_MORNING_AUTO

    def test_target_dropping_below_soc_ends_the_charge(self):
        sch = _schedule(_auto())
        sch.set_morning_target(_target(40))
        sch.compute_operation(_at(6, 40), 20.0, is_workday=True)
        sch.set_morning_target(_target(25))
        _, events = sch.compute_operation(_at(6, 45), 26.0, is_workday=True)
        ended = next(e for e in events if isinstance(e, SlotDisengaged))
        assert ended.reason is DisengageReason.TARGET_REACHED
        assert ended.target_soc == 25.0


class TestWorkdayGate:
    def test_day_off_never_starts(self):
        sch = _schedule(_auto())
        sch.set_morning_target(_target(40))
        _, events = sch.compute_operation(_at(6, 50), 20.0, is_workday=False)
        assert not any(isinstance(e, SlotEngaged) for e in events)

    def test_unknown_calendar_counts_as_workday(self):
        sch = _schedule(_auto())
        sch.set_morning_target(_target(40))
        _, events = sch.compute_operation(_at(6, 50), 20.0, is_workday=None)
        assert _engaged(events).slot is SlotKind.CHARGE_MORNING_AUTO

    def test_manual_morning_charge_ignores_the_gate(self):
        manual = BatteryScheduleEntry(
            kind=SlotKind.CHARGE_MORNING,
            enabled=True,
            start=time(4, 0),
            end=time(7, 0),
            target_soc=40.0,
            behavior=SlotBehavior.IMMEDIATE,
        )
        sch = _schedule(manual)
        _, events = sch.compute_operation(_at(5, 0), 20.0, is_workday=False)
        assert _engaged(events).slot is SlotKind.CHARGE_MORNING


class TestManualBeatsAuto:
    def test_both_ready_manual_wins(self):
        manual = BatteryScheduleEntry(
            kind=SlotKind.CHARGE_MORNING,
            enabled=True,
            start=time(4, 0),
            end=time(7, 0),
            target_soc=50.0,
            behavior=SlotBehavior.IMMEDIATE,
        )
        sch = _schedule(_auto(), manual)
        sch.set_morning_target(_target(40))
        _, events = sch.compute_operation(_at(6, 50), 20.0, is_workday=True)
        assert _engaged(events).slot is SlotKind.CHARGE_MORNING


class TestPersistence:
    def test_target_round_trips(self):
        sch = _schedule(_auto())
        sch.set_morning_target(_target(42, source="tomorrow_live"))
        restored = BatterySchedule.from_dict(sch.to_dict())
        assert restored.morning_target == _target(42, source="tomorrow_live")

    def test_store_without_new_fields_restores_defaults(self):
        data = BatterySchedule().to_dict()
        data.pop("morning_target")
        for scope in ("today", "tomorrow"):
            data[scope].pop(SlotKind.CHARGE_MORNING_AUTO.name)
        restored = BatterySchedule.from_dict(data)
        assert restored.morning_target is None
        auto = restored.today_entry_for(SlotKind.CHARGE_MORNING_AUTO)
        assert (auto.enabled, auto.start, auto.end, auto.target_soc) == (
            False,
            time(4, 0),
            time(7, 0),
            60.0,
        )

    def test_set_same_target_reports_no_change(self):
        sch = BatterySchedule()
        assert sch.set_morning_target(_target(40)) is True
        assert sch.set_morning_target(_target(40)) is False


class TestResolveMorningTarget:
    """Which forecast variant feeds the remembered target, by time of day."""

    @staticmethod
    def _catalog() -> SimpleNamespace:
        return SimpleNamespace(
            target_socs={
                PvForecast.LIVE: TargetSoc(
                    variant=PvForecast.LIVE, flat=_result(31), prev_days=[]
                ),
                PvForecast.TOMORROW_LIVE: TargetSoc(
                    variant=PvForecast.TOMORROW_LIVE, flat=_result(44), prev_days=[]
                ),
            }
        )

    def test_before_seven_targets_this_morning(self):
        target = BatteryScheduleService._resolve_morning_target(  # noqa: SLF001
            _at(3, 0), self._catalog()
        )
        assert target is not None
        assert (target.day, target.value, target.source) == (DAY, 31, "live")

    def test_hour_after_deadline_keeps_this_morning(self):
        """07:00-08:00: no push, so the 07:00 window-end telegram is not a fallback."""
        assert (
            BatteryScheduleService._resolve_morning_target(  # noqa: SLF001
                _at(7, 30), self._catalog()
            )
            is None
        )

    def test_from_eight_targets_tomorrow(self):
        target = BatteryScheduleService._resolve_morning_target(  # noqa: SLF001
            _at(8, 0), self._catalog()
        )
        assert target is not None
        assert (target.day, target.value, target.source) == (
            date(2026, 10, 13),
            44,
            "tomorrow_live",
        )


class TestNotification:
    """The telegram names the target and shouts when it is the fallback."""

    @staticmethod
    def _message(source: str | None, target: float | None = 40.0) -> str:
        from custom_components.smart_rce.infrastructure.battery_schedule_notifier import (  # noqa: PLC0415
            BatteryScheduleNotifier,
        )

        rendered = BatteryScheduleNotifier._render(  # noqa: SLF001
            SlotEngaged(
                slot=SlotKind.CHARGE_MORNING_AUTO,
                soc=20.0,
                at=_at(6, 40),
                target_soc=target,
                target_source=source,
            )
        )
        assert rendered is not None
        return rendered[1]

    def test_forecast_target_names_its_source(self):
        assert self._message("live").endswith("target 40% (from live).")

    def test_fallback_is_flagged(self):
        assert "FALLBACK" in self._message("fallback", 60.0)

    def test_fixed_slot_shows_plain_target(self):
        assert self._message(None).endswith("target 40%.")
