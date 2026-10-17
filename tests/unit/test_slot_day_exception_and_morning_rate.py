"""Workdays-only slots on 17.10.2026 (one-off) and the morning discharge pace."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from custom_components.smart_rce.domain.battery_schedule import (
    BatterySchedule,
    BatteryScheduleEntry,
    ReserveWindow,
    SlotBehavior,
    SlotEngaged,
    SlotKind,
    T1SocReserve,
)
import pytest

TZ = timezone(timedelta(hours=2))
SATURDAY = date(2026, 10, 17)
AUTO_KINDS = (SlotKind.CHARGE_MORNING_AUTO, SlotKind.DISCHARGE_MORNING_AUTO)


class TestDayException:
    @pytest.mark.parametrize("kind", AUTO_KINDS)
    def test_both_morning_auto_slots_run_on_17_10_2026(self, kind):
        assert kind.profile.runs_on(SATURDAY, is_workday=False)

    @pytest.mark.parametrize(
        "day", [date(2026, 10, 18), date(2025, 10, 17), date(2027, 10, 17)]
    )
    def test_only_that_date_not_the_day_of_any_year(self, day):
        assert not SlotKind.CHARGE_MORNING_AUTO.profile.runs_on(day, is_workday=False)

    def test_exception_concerns_only_the_workdays_only_slots(self):
        # The morning discharge runs every day since 17.10.2026.
        assert {k for k in SlotKind if k.profile.workdays_only} == {
            SlotKind.CHARGE_MORNING_AUTO
        }

    def test_night_charge_engages_on_the_saturday(self):
        entry = BatteryScheduleEntry(
            kind=SlotKind.CHARGE_MORNING_AUTO,
            enabled=True,
            start=time(4, 0),
            end=time(7, 0),
            target_soc=60.0,
            behavior=SlotBehavior.DELAYED_TO_END,
        )
        today = BatteryScheduleEntry.defaults_for_all_kinds()
        today[entry.kind] = entry
        sch = BatterySchedule(today=today)
        reserve = T1SocReserve(
            day=SATURDAY,
            value=40,
            flat=40,
            prev_without_worst=None,
            dip_kwh=None,
            source="live_soft",
            window=ReserveWindow.FULL,
        )
        at = datetime(2026, 10, 17, 6, 30, tzinfo=TZ)
        sch.set_t1_reserve(reserve, at - timedelta(hours=3))
        _, events = sch.compute_operation(at, 10.0, is_workday=False)
        engaged = [e for e in events if isinstance(e, SlotEngaged)]
        assert engaged and engaged[0].slot is SlotKind.CHARGE_MORNING_AUTO
        _, none = BatterySchedule(today=today).compute_operation(
            at.replace(day=18), 10.0, is_workday=False
        )
        assert none == []


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
