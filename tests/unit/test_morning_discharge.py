"""Automatic morning discharge: rule, schedule wiring, service (15.10.2026)."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from types import SimpleNamespace

from custom_components.smart_rce.application.morning_discharge_service import (
    MorningDischargeService,
)
from custom_components.smart_rce.domain.battery_schedule import (
    BatterySchedule,
    BatteryScheduleEntry,
    MorningDischargeBranch,
    MorningDischargePlan,
    ReserveWindow,
    SlotEngaged,
    SlotKind,
    T1SocReserve,
)
from custom_components.smart_rce.domain.charge_curve import kwh_to_full
from custom_components.smart_rce.domain.charge_start_shift import (
    find_candidate,
    lowest_soc_the_sun_fills,
)
from custom_components.smart_rce.domain.evening_plan import EveningPlan
from custom_components.smart_rce.domain.pv_surplus import (
    SurplusProfile,
    slots_from_hourly,
)
from custom_components.smart_rce.domain.rce import RceDayPrices
import pytest

TZ = timezone.utc
DAY = date(2026, 10, 15)  # Thursday
H = 60
# Net RCE: dear 07-09 (07 a little less than 08), cheap midday.
PRICES = (
    700, 700, 700, 700, 700, 700, 900, 1000, 1100, 800, 600, 500,
    450, 500, 600, 700, 900, 1200, 1500, 1400, 1300, 900, 800, 750,
)  # fmt: skip


def _plan(reserve: int, fill: int, gross: float) -> MorningDischargePlan:
    return MorningDischargePlan.decide(
        day=DAY, hour=8, gross_price=gross, reserve=reserve, fill=fill
    )


class TestDecide:
    def test_sun_enough_stands_on_the_reserve_at_any_price(self):
        plan = _plan(reserve=25, fill=20, gross=500.0)
        assert (plan.target, plan.branch) == (25, MorningDischargeBranch.SUN_ENOUGH)

    def test_below_the_bar_only_the_room_the_sun_refills(self):
        plan = _plan(reserve=25, fill=42, gross=749.0)
        assert (plan.target, plan.branch) == (42, MorningDischargeBranch.ROOM)

    def test_at_the_bar_arbitrage_up_to_ten_below_the_fill(self):
        plan = _plan(reserve=25, fill=42, gross=750.0)
        assert (plan.target, plan.branch) == (35, MorningDischargeBranch.ARBITRAGE)

    def test_arbitrage_never_below_the_fill_when_it_is_close(self):
        assert _plan(reserve=25, fill=30, gross=1300.0).target == 30

    def test_bar_matches_the_evening_plan(self):
        assert MorningDischargePlan.EXPORT_THRESHOLD == EveningPlan.EXPORT_THRESHOLD


class TestBestHour:
    PRICES_DAY = RceDayPrices(None, DAY, PRICES)

    def test_dearest_whole_hour_from_seven(self):
        assert MorningDischargePlan.best_hour(self.PRICES_DAY, 6, 10) == 8

    def test_hour_must_end_by_the_charge_start(self):
        assert MorningDischargePlan.best_hour(self.PRICES_DAY, 7, 8) == 7

    def test_no_hour_when_the_charge_starts_at_seven(self):
        assert MorningDischargePlan.best_hour(self.PRICES_DAY, 7, 7) is None


def test_lowest_soc_the_sun_fills_walks_up_from_the_floor():
    # 1 kWh/h surplus 10-16 (PV 1.9 minus flat 0.9): ~6 kWh, not enough from 20%.
    pv = slots_from_hourly(dict.fromkeys(range(10, 16), 1.9))
    profile = SurplusProfile.build(DAY, pv, None)
    fill = lowest_soc_the_sun_fills(profile, PRICES, 9 * H, 11 * H, 20)
    assert 20 < fill < 100
    # At the fill the start rule says "enough", one point below it does not.
    assert find_candidate(profile, PRICES, 9 * H, kwh_to_full(fill), 11 * H).enough
    assert not find_candidate(
        profile, PRICES, 9 * H, kwh_to_full(fill - 1), 11 * H
    ).enough


def _reserve(value: int) -> T1SocReserve:
    return T1SocReserve(
        day=DAY,
        value=value,
        flat=value,
        prev_without_worst=None,
        dip_kwh=None,
        source="live_soft",
        window=ReserveWindow.REMAINING,
    )


def _auto_discharge(enabled: bool = True) -> BatteryScheduleEntry:
    return BatteryScheduleEntry.default_for(
        SlotKind.DISCHARGE_MORNING_AUTO, enabled=enabled
    )


def _at(h: int, m: int = 0) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=TZ)


class TestSchedule:
    def _schedule(self, entry: BatteryScheduleEntry) -> BatterySchedule:
        today = BatteryScheduleEntry.defaults_for_all_kinds()
        today[entry.kind] = entry
        return BatterySchedule(today=today)

    def test_without_a_plan_the_slot_stands_still(self):
        sch = self._schedule(_auto_discharge())
        op, events = sch.compute_operation(_at(8, 50), 80.0, is_workday=True)
        assert events == [] and sch.currently_engaging is None

    def test_plan_discharges_in_its_hour_to_its_target(self):
        sch = self._schedule(_auto_discharge())
        sch.set_morning_discharge(_plan(reserve=25, fill=42, gross=1300.0))
        # 80 -> 35% before 09:00: DELAYED_TO_END engages in the hour, not at 7.
        _, early = sch.compute_operation(_at(7, 30), 80.0, is_workday=True)
        assert early == []
        _, events = sch.compute_operation(_at(8, 30), 80.0, is_workday=True)
        engaged = [e for e in events if isinstance(e, SlotEngaged)]
        assert engaged and engaged[0].slot is SlotKind.DISCHARGE_MORNING_AUTO
        assert engaged[0].target_soc == 35.0
        assert engaged[0].target_source == (
            "arbitrage 08-09, 1300 brutto, reserve 25, sun fills 42"
        )

    def test_engages_four_minutes_before_the_rate_model(self):
        # 38 -> 35% = 3 pp x 75 s = 225 s; with the 240 s lead the 08-09 hour
        # starts at 08:52:15 instead of 08:56:15 (room for a lower target).
        sch = self._schedule(_auto_discharge())
        sch.set_morning_discharge(_plan(reserve=25, fill=42, gross=1300.0))
        _, before = sch.compute_operation(_at(8, 51), 38.0, is_workday=True)
        assert before == []
        _, events = sch.compute_operation(_at(8, 53), 38.0, is_workday=True)
        engaged = [e for e in events if isinstance(e, SlotEngaged)]
        assert engaged and engaged[0].slot is SlotKind.DISCHARGE_MORNING_AUTO

    def test_not_on_a_day_off(self):
        sch = self._schedule(_auto_discharge())
        sch.set_morning_discharge(_plan(reserve=25, fill=42, gross=1300.0))
        _, events = sch.compute_operation(_at(8, 30), 80.0, is_workday=False)
        assert events == []

    def test_disabled_slot_ignores_the_plan(self):
        sch = self._schedule(_auto_discharge(enabled=False))
        sch.set_morning_discharge(_plan(reserve=25, fill=42, gross=1300.0))
        _, events = sch.compute_operation(_at(8, 30), 80.0, is_workday=True)
        assert events == []


class _Surplus:
    def __init__(self, rate: float) -> None:
        self.rate = rate

    def today_profile(self, *, history: bool, pv_scale: float, source=None):
        pv = slots_from_hourly(dict.fromkeys(range(9, 16), self.rate))
        return SurplusProfile.build(DAY, pv, None)


def _service(rate: float, *, s0: time = time(11, 0), workday=True, now=None):
    plans: list = []
    entry = _auto_discharge()
    schedule = SimpleNamespace(
        slot=lambda scope, kind: entry,
        t1_reserve=_reserve(25),
        set_morning_discharge=plans.append,
    )
    ems = SimpleNamespace(
        battery_schedule_service=schedule,
        is_workday_today=workday,
        charge_slots=SimpleNamespace(today_start=s0),
        rce_prices=SimpleNamespace(
            rce_prices=SimpleNamespace(today=RceDayPrices(None, DAY, PRICES))
        ),
    )
    clock = now or _at(6, 30)
    service = MorningDischargeService(ems, _Surplus(rate), lambda: clock)
    service.refresh()
    return plans[-1]


class TestService:
    def test_plenty_of_sun_stands_on_the_reserve_in_the_dearest_hour(self):
        plan = _service(6.0)
        # Reserve 25 without margin; the 08:00 hour stands on the 08 step (+5).
        assert (plan.hour, plan.target, plan.branch) == (
            8,
            30,
            MorningDischargeBranch.SUN_ENOUGH,
        )
        assert plan.gross_price == pytest.approx(1100 * 1.23)

    def test_moves_on_to_the_next_hour_that_pays_off(self):
        # 09 (800) is dearer than every PV hour 11-15 (max 700); 10 (600) is not.
        assert _service(6.0).hours == (8, 9)
        assert _service(6.0, now=_at(9, 20)).hour == 9
        assert _service(6.0, now=_at(10, 5)) is None

    def test_little_sun_above_the_bar_is_arbitrage(self):
        plan = _service(1.4)
        assert plan.branch is MorningDischargeBranch.ARBITRAGE
        assert plan.target == min(plan.reserve + 10, plan.fill)

    def test_an_hour_cheaper_than_the_pv_window_is_left_alone(self):
        # S0 08:00: only 07 is searched, and 08 (1100) sells the sun dearer.
        assert _service(6.0, s0=time(8, 0)) is None

    def test_no_plan_on_a_day_off(self):
        assert _service(6.0, workday=False) is None


def test_auto_discharge_calls_the_phone():
    from custom_components.smart_rce.domain.battery_schedule import NotificationLevel

    assert (
        SlotKind.DISCHARGE_MORNING_AUTO.profile.notification_level
        is NotificationLevel.EMERGENCY
    )
