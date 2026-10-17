"""Workdays-only slots and the morning discharge pace."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from custom_components.smart_rce.domain.battery_schedule import (
    BatterySchedule,
    BatteryScheduleEntry,
    SlotBehavior,
    SlotEngaged,
    SlotKind,
)
import pytest

TZ = timezone(timedelta(hours=2))
SATURDAY = date(2026, 10, 17)


class TestWorkdaysOnly:
    def test_the_charge_slots_for_t1_and_t2_are_workdays_only(self):
        # The morning discharge runs every day since 17.10.2026; the afternoon
        # charge only on workdays (no T2 on a day off).
        assert {k for k in SlotKind if k.profile.workdays_only} == {
            SlotKind.CHARGE_MORNING_AUTO,
            SlotKind.CHARGE_AFTERNOON,
        }

    def test_afternoon_charge_stays_still_on_a_day_off(self):
        entry = BatteryScheduleEntry(
            kind=SlotKind.CHARGE_AFTERNOON,
            enabled=True,
            start=time(13, 0),
            end=time(16, 0),
            target_soc=80.0,
            behavior=SlotBehavior.IMMEDIATE,
        )
        today = BatteryScheduleEntry.defaults_for_all_kinds()
        today[entry.kind] = entry
        at = datetime(2026, 10, 17, 14, 0, tzinfo=TZ)
        _, off = BatterySchedule(today=today).compute_operation(
            at, 40.0, is_workday=False
        )
        assert not [e for e in off if isinstance(e, SlotEngaged)]
        _, on = BatterySchedule(today=today).compute_operation(
            at, 40.0, is_workday=True
        )
        assert [e.slot for e in on if isinstance(e, SlotEngaged)] == [
            SlotKind.CHARGE_AFTERNOON
        ]

    def test_the_one_off_saturday_is_gone(self):
        assert not SlotKind.CHARGE_MORNING_AUTO.profile.runs_on(
            SATURDAY, is_workday=False
        )


class TestMorningDischargePace:
    def test_morning_model_holds_21_for_nine_minutes(self):
        # 30->21: 9 x 75; 21->20: 540; 20->19: 10; 19->12: 7 x 60.
        assert SlotKind.DISCHARGE_MORNING_AUTO.profile.seconds_for_soc_traversal(
            30.0, 12.0
        ) == pytest.approx(9 * 75 + 540 + 10 + 7 * 60)

    def test_other_discharge_slots_keep_the_evening_model(self):
        evening = SlotKind.DISCHARGE_EVENING_EARLY.profile
        assert evening.rate_zones is None
        assert evening.seconds_for_soc_traversal(30.0, 12.0) == pytest.approx(
            5 * 75 + 9 * 36 + 2 * 97 + 2 * 34
        )

    def test_16_10_would_have_started_at_08_28(self):
        # Target 10% from SoC 30% before 09:00 with the 4-min lead.
        entry = BatteryScheduleEntry(
            kind=SlotKind.DISCHARGE_MORNING_AUTO,
            enabled=True,
            start=time(8, 0),
            end=time(9, 0),
            target_soc=10.0,
            behavior=SlotBehavior.DELAYED_TO_END,
        )

        def at(m):
            return datetime(2026, 10, 16, 8, m, tzinfo=TZ)

        assert not entry.should_apply_now(at(26), 30.0)
        assert entry.should_apply_now(at(28), 30.0)
