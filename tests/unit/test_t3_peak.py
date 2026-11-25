"""T3PeakPlan — sell in the afternoon T3 peak, buy back before T2 (10.11.2026)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from custom_components.smart_rce.domain.battery_schedule import (
    BatterySchedule,
    BatteryScheduleEntry,
    SlotBehavior,
    SlotKind,
    T3PeakEvaluation,
    T3PeakHour,
    T3PeakPlan,
)
from custom_components.smart_rce.domain.battery_schedule.events import (
    SlotDisengaged,
    SlotEngaged,
)
import pytest

DAY = date(2026, 11, 10)  # Tuesday
TZ = timezone(timedelta(hours=1))
# RCE 10.11.2026, gross zl/kWh.
RCE = (
    1.22, 1.38, 1.36, 1.33, 1.35, 1.50, 2.28, 2.54, 2.59, 2.47, 2.37, 2.60,
    3.60, 3.89, 4.50, 4.39, 3.40, 3.18, 2.62, 2.65, 2.61, 1.55, 1.29, 1.03,
)  # fmt: skip
T1, T2, T3 = (0.58, 0.324), (0.963, 0.533), (0.524, 0.102)


def _hours(rce=RCE, surplus=0.0) -> list[T3PeakHour]:
    def zone(h):
        return T1 if 7 <= h < 13 else T2 if 16 <= h < 21 else T3

    return [
        T3PeakHour(
            surplus_kwh=surplus if 13 <= h < 16 else 0.0,
            deficit_kwh=0.0 if surplus else 0.8,
            rce_gross=rce[h],
            buy_energy=zone(h)[0],
            buy_distribution=zone(h)[1],
        )
        for h in range(24)
    ]


def _evaluate(soc: float, rce=RCE, surplus=0.0) -> T3PeakEvaluation:
    return T3PeakEvaluation.evaluate(
        DAY,
        _hours(rce, surplus),
        soc=soc,
        t2_start=16,
        charge_end_minute=15 * 60 + 55,
        t2_hours=range(16, 21),
        t2_buy=sum(T2),
    )


class TestEvaluate:
    def test_picks_the_better_variant(self):
        evaluation = _evaluate(95.0)
        best = max(evaluation.variants, key=lambda v: v.gain_zl)
        assert evaluation.plan is not None
        assert evaluation.plan.sell_hour == best.sell_hour
        assert evaluation.plan.gain_zl >= T3PeakPlan.MIN_GAIN_ZL

    def test_peak_at_14_charges_first_and_keeps_80_for_t2(self):
        v14 = next(v for v in _evaluate(95.0).variants if v.sell_hour == 14)
        # 15:00-15:55 at 75 s/pp (less the 4 min lead) reaches 80 from 40.
        assert (v14.pre_charge, v14.target) == (True, 40)
        assert v14.end_soc >= T3PeakPlan.END_SOC
        assert v14.eligible

    def test_both_variants_are_valued(self):
        variants = {v.sell_hour: v for v in _evaluate(88.0).variants}
        assert set(variants) == {13, 14}
        assert variants[13].target == T3PeakPlan.FLOOR
        assert variants[13].end_soc == pytest.approx(100.0)
        assert variants[14].end_soc >= T3PeakPlan.END_SOC

    def test_an_empty_battery_sells_at_13_and_charges_after(self):
        # From 70% the 13:00 sale and two hours of charge beat the 14:00 one.
        plan = _evaluate(70.0).plan
        assert plan is not None and plan.sell_hour == 13 and not plan.pre_charge

    def test_a_dearer_t2_hour_no_longer_blocks_a_paying_round_trip(self):
        # 25.11.2026 (net x 1.23): 14:00 2637, 16:00 2657 — 0.7% short of the
        # old "dearer than every T2 hour" bar; the round trip still pays.
        net = (
            917, 953, 931, 923, 924, 946, 1333, 1845, 1810, 1531, 1244, 1220,
            1443, 1900, 2637, 2720, 2657, 2074, 2098, 2225, 2392, 1580, 1089, 965,
        )  # fmt: skip
        evaluation = _evaluate(100.0, rce=tuple(p * 1.23 / 1000 for p in net))
        v14 = next(v for v in evaluation.variants if v.sell_hour == 14)
        assert not v14.dearer_than_t2
        assert v14.eligible and v14.gain_zl >= T3PeakPlan.MIN_GAIN_ZL
        assert evaluation.plan is not None and evaluation.plan.sell_hour == 14

    def test_no_sale_below_the_gain_bar(self):
        rce = list(RCE)
        rce[13], rce[14], rce[15] = 1.0, 1.05, 0.9
        rce[16:21] = [1.0, 0.95, 0.9, 0.9, 0.9]
        evaluation = _evaluate(88.0, rce=tuple(rce))
        assert evaluation.plan is None
        assert all(v.gain_zl < T3PeakPlan.MIN_GAIN_ZL for v in evaluation.variants)

    def test_to_dict_carries_the_plan_and_variants(self):
        d = _evaluate(88.0).to_dict()
        assert d["plan"]["sell_hour"] in (13, 14)
        assert [v["sell_hour"] for v in d["variants"]] == [13, 14]


def _at(h: int, m: int = 0) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=TZ)


def _schedule(plan: T3PeakPlan, *, sale_enabled: bool = True) -> BatterySchedule:
    today = BatteryScheduleEntry.defaults_for_all_kinds()
    today[SlotKind.CHARGE_AFTERNOON] = BatteryScheduleEntry(
        kind=SlotKind.CHARGE_AFTERNOON,
        enabled=True,
        start=time(13, 0),
        end=time(15, 55),
        target_soc=100.0,
    )
    today[SlotKind.DISCHARGE_T3_PEAK] = BatteryScheduleEntry.default_for(
        SlotKind.DISCHARGE_T3_PEAK, enabled=sale_enabled
    )
    sch = BatterySchedule(today=today)
    sch.set_t3_peak(DAY, plan)
    return sch


def _plan(hour: int) -> T3PeakPlan:
    return T3PeakPlan(
        day=DAY, sell_hour=hour, target=40 if hour == 14 else 10,
        pre_charge=hour == 14, gain_zl=12.0,
    )  # fmt: skip


def _engaged(sch: BatterySchedule, now: datetime, soc: float) -> SlotKind | None:
    _, events = sch.compute_operation(now, soc, is_workday=True)
    started = [e.slot for e in events if isinstance(e, SlotEngaged)]
    return started[-1] if started else None


class TestSchedule:
    def test_peak_at_14_hands_over_charge_sell_charge(self):
        sch = _schedule(_plan(14))
        assert _engaged(sch, _at(13, 0), 85.0) is SlotKind.CHARGE_AFTERNOON
        # 13:59 still charging (sticky), 14:00 the window ends -> the sale.
        assert _engaged(sch, _at(13, 59), 99.0) is None
        assert _engaged(sch, _at(14, 0), 99.0) is SlotKind.DISCHARGE_T3_PEAK
        assert _engaged(sch, _at(14, 30), 70.0) is None
        assert _engaged(sch, _at(15, 0), 52.0) is SlotKind.CHARGE_AFTERNOON

    def test_the_sale_reaching_its_floor_waits_for_15(self):
        sch = _schedule(_plan(14))
        _engaged(sch, _at(14, 0), 99.0)
        _, events = sch.compute_operation(_at(14, 40), 40.0, is_workday=True)
        assert any(isinstance(e, SlotDisengaged) for e in events)
        assert _engaged(sch, _at(14, 41), 40.0) is None
        assert _engaged(sch, _at(15, 0), 40.0) is SlotKind.CHARGE_AFTERNOON

    def test_peak_at_13_sells_then_charges_from_14(self):
        sch = _schedule(_plan(13))
        assert _engaged(sch, _at(13, 0), 88.0) is SlotKind.DISCHARGE_T3_PEAK
        assert _engaged(sch, _at(14, 0), 40.0) is SlotKind.CHARGE_AFTERNOON

    def test_the_sale_runs_immediate_at_its_target(self):
        entry = _schedule(_plan(14))._effective_entry(SlotKind.DISCHARGE_T3_PEAK, DAY)
        assert (entry.start, entry.end, entry.target_soc) == (time(14), time(15), 40)
        assert entry.behavior is SlotBehavior.IMMEDIATE

    def test_a_switched_off_sale_leaves_the_afternoon_charge_alone(self):
        sch = _schedule(_plan(14), sale_enabled=False)
        entry = sch._effective_entry(SlotKind.CHARGE_AFTERNOON, DAY, _at(13, 0))
        assert (entry.start, entry.end) == (time(13), time(15, 55))
        assert entry.behavior is SlotBehavior.DELAYED_TO_END
        assert _engaged(sch, _at(14, 0), 99.0) is None

    def test_without_a_plan_nothing_changes(self):
        sch = _schedule(_plan(14))
        sch.set_t3_peak(DAY, None)
        entry = sch._effective_entry(SlotKind.DISCHARGE_T3_PEAK, DAY)
        assert not entry.enabled

    def test_the_latch_survives_a_restart(self):
        sch = _schedule(_plan(14))
        assert sch.latch_t3_peak(_plan(14))
        restored = BatterySchedule.from_dict(sch.to_dict())
        assert restored.t3_peak_latched(DAY)
        assert restored.t3_peak_for(DAY) == _plan(14)
        assert not restored.t3_peak_latched(DAY + timedelta(days=1))

    def test_a_broken_latch_is_dropped(self):
        data = _schedule(_plan(14)).to_dict() | {"t3_peak_latch": {"day": "x"}}
        assert BatterySchedule.from_dict(data).t3_peak_for(DAY) is None


def _day_off_plan(day: date = DAY) -> T3PeakPlan:
    return T3PeakPlan(
        day=day, sell_hour=16, target=10, pre_charge=True, gain_zl=5.0,
        day_off=True, charge_to=68,
    )  # fmt: skip


def _engaged_off(sch: BatterySchedule, now: datetime, soc: float) -> SlotKind | None:
    _, events = sch.compute_operation(now, soc, is_workday=False)
    started = [e.slot for e in events if isinstance(e, SlotEngaged)]
    return started[-1] if started else None


class TestDayOff:
    """A day off sells in its dearest hour after a charge the hour before (25.11)."""

    def test_charges_the_hour_before_then_sells_then_nothing(self):
        sch = _schedule(_day_off_plan())
        assert _engaged_off(sch, _at(14, 0), 20.0) is None
        assert _engaged_off(sch, _at(15, 0), 20.0) is SlotKind.CHARGE_AFTERNOON
        entry = sch._effective_entry(SlotKind.CHARGE_AFTERNOON, DAY, _at(15, 0))
        assert (entry.start, entry.end, entry.target_soc) == (time(15), time(16), 68)
        assert _engaged_off(sch, _at(16, 0), 68.0) is SlotKind.DISCHARGE_T3_PEAK
        assert _engaged_off(sch, _at(17, 0), 12.0) is None

    def test_without_a_plan_the_afternoon_charge_stays_out(self):
        sch = _schedule(_day_off_plan())
        sch.set_t3_peak(DAY, None)
        assert _engaged_off(sch, _at(13, 30), 20.0) is None

    def test_an_overlapping_pair_gives_way_and_a_later_one_stays(self):
        sch = _schedule(_day_off_plan())
        early = SlotKind.DISCHARGE_EVENING_EARLY
        sch._today[early] = BatteryScheduleEntry(
            kind=early, enabled=True, start=time(15), end=time(17), target_soc=10.0
        )
        assert not sch._effective_entry(early, DAY).enabled
        sch._today[early] = BatteryScheduleEntry(
            kind=early, enabled=True, start=time(18), end=time(20), target_soc=10.0
        )
        assert sch._effective_entry(early, DAY).enabled

    def test_the_plan_round_trips(self):
        plan = _day_off_plan()
        assert T3PeakPlan.from_dict(plan.to_dict()) == plan
        assert "day-off peak: charge 15-16 to 68%" in plan.describe()

    def test_the_evaluation_charges_to_68_and_sells_to_the_floor(self):
        hours = _hours(HOLIDAY_RCE)
        evaluation = T3PeakEvaluation.evaluate_day_off(
            HOLIDAY, hours, soc=10.0, sell_hour=15, moved_value=sum(T3)
        )
        assert evaluation.plan is not None
        assert evaluation.plan.charge_to == T3PeakPlan.DAY_OFF_CHARGE_TO
        assert evaluation.variants[0].end_soc == pytest.approx(10.0, abs=1)
        assert evaluation.plan.gain_zl >= T3PeakPlan.MIN_GAIN_ZL

    def test_a_full_battery_sells_without_charging(self):
        evaluation = T3PeakEvaluation.evaluate_day_off(
            HOLIDAY, _hours(HOLIDAY_RCE), soc=90.0, sell_hour=15, moved_value=sum(T3)
        )
        assert not evaluation.variants[0].pre_charge


class TestChart:
    """The T3 runs stand on the chart ahead, as the night charge does (25.11)."""

    def test_a_workday_shows_charge_sale_charge(self):
        sch = _schedule(_plan(14))
        runs = sch.upcoming_runs(_at(9, 0), 90.0, is_workday=True)
        t3 = [
            (r.slot, r.window_start.time(), r.end.time(), r.target_soc)
            for r in runs
            if r.slot in (SlotKind.CHARGE_AFTERNOON, SlotKind.DISCHARGE_T3_PEAK)
        ]
        assert t3 == [
            (SlotKind.CHARGE_AFTERNOON, time(13), time(14), 100.0),
            (SlotKind.DISCHARGE_T3_PEAK, time(14), time(15), 40.0),
            (SlotKind.CHARGE_AFTERNOON, time(15), time(15, 55), 100.0),
        ]

    def test_the_charge_before_the_sale_shows_after_a_full_night(self):
        # The night charge heads for 100%; the house drains T1 unseen by the chain.
        sch = _schedule(_plan(14))
        runs = sch.upcoming_runs(_at(7, 5), 100.0, is_workday=True)
        assert (SlotKind.CHARGE_AFTERNOON, time(13)) in [
            (r.slot, r.window_start.time()) for r in runs
        ]

    def test_during_the_sale_only_what_is_left_shows(self):
        sch = _schedule(_plan(14))
        runs = sch.upcoming_runs(_at(14, 20), 70.0, is_workday=True)
        slots = [(r.slot, r.window_start.time()) for r in runs]
        assert (SlotKind.CHARGE_AFTERNOON, time(13)) not in slots
        assert (SlotKind.CHARGE_AFTERNOON, time(15)) in slots

    def test_a_day_off_shows_charge_and_sale(self):
        sch = _schedule(_day_off_plan())
        runs = sch.upcoming_runs(_at(9, 0), 20.0, is_workday=False)
        t3 = [
            (r.slot, r.window_start.time(), r.target_soc)
            for r in runs
            if r.slot in (SlotKind.CHARGE_AFTERNOON, SlotKind.DISCHARGE_T3_PEAK)
        ]
        assert t3 == [
            (SlotKind.CHARGE_AFTERNOON, time(15), 68.0),
            (SlotKind.DISCHARGE_T3_PEAK, time(16), 10.0),
        ]

    def test_tomorrow_shows_its_preview(self):
        tomorrow = DAY + timedelta(days=1)
        sch = _schedule(_plan(14))
        sch.set_t3_peak(DAY, None)
        sch.set_t3_peak(tomorrow, _day_off_plan(tomorrow))
        runs = sch.upcoming_runs(_at(21, 0), 30.0, is_workday=False, day=tomorrow)
        assert any(r.slot is SlotKind.DISCHARGE_T3_PEAK for r in runs)


class _Surplus:
    def live_soft_profile(self, day, *, tomorrow: bool):
        from custom_components.smart_rce.domain.pv_surplus import SurplusProfile

        return SurplusProfile.build(day, {}, None)  # no sun, flat 0.9 kWh/h


def _service(  # noqa: PLR0913 — the test's knobs
    now: datetime,
    schedule,
    *,
    workday=True,
    soc=88.0,
    rce=RCE,
    tomorrow_rce=None,
    workday_tomorrow=True,
):
    from types import SimpleNamespace

    from custom_components.smart_rce.application.t3_peak_service import T3PeakService
    from custom_components.smart_rce.domain.rce import RceDayPrices

    service = SimpleNamespace(
        slot=lambda scope, kind: schedule._today[kind],
        set_t3_peak=schedule.set_t3_peak,
        t3_peak_latched=schedule.t3_peak_latched,
        latch_t3_peak=schedule.latch_t3_peak,
        set_t3_peak_report=lambda day, evaluation, why: None,
        export_threshold=850.0,
    )
    day = now.date()
    tomorrow = (
        RceDayPrices(None, day + timedelta(days=1), _net(tomorrow_rce))
        if tomorrow_rce is not None
        else None
    )
    ems = SimpleNamespace(
        battery_schedule_service=service,
        is_workday_today=workday,
        is_workday_tomorrow=workday_tomorrow,
        last_input_state=SimpleNamespace(battery_soc=soc),
        rce_prices=SimpleNamespace(
            rce_prices=SimpleNamespace(
                today=RceDayPrices(None, day, _net(rce)), tomorrow=tomorrow
            )
        ),
    )
    T3PeakService(ems, _Surplus(), lambda: now).refresh()


def _net(gross_zl_kwh) -> tuple[float, ...]:
    return tuple(p / 1.23 * 1000 for p in gross_zl_kwh)


# 11.11.2026, a holiday (Wednesday): RCE gross zl/kWh, 15:00 the day's peak.
HOLIDAY = date(2026, 11, 11)
HOLIDAY_RCE = (
    1.10, 1.05, 1.00, 1.00, 1.02, 1.08, 1.15, 1.25, 1.30, 1.35, 1.40, 1.50,
    1.70, 1.85, 2.00, 2.21, 2.05, 1.90, 1.75, 1.60, 1.45, 1.30, 1.20, 1.10,
)  # fmt: skip


class TestService:
    def test_plans_in_the_morning_and_latches_at_13(self):
        sch = _schedule(_plan(14))
        sch.set_t3_peak(DAY, None)
        _service(_at(9, 0), sch)
        assert sch.t3_peak_for(DAY) is not None
        assert not sch.t3_peak_latched(DAY)
        _service(_at(13, 0), sch)
        latched = sch.t3_peak_for(DAY)
        assert sch.t3_peak_latched(DAY)
        # The half-sold battery at 14:30 does not move the latched plan.
        _service(_at(14, 30), sch, soc=60.0)
        assert sch.t3_peak_for(DAY) == latched

    def test_no_new_plan_after_13_15(self):
        sch = _schedule(_plan(14))
        sch.set_t3_peak(DAY, None)
        _service(_at(13, 20), sch)
        assert sch.t3_peak_for(DAY) is None

    def test_a_day_off_sells_in_its_dearest_hour_and_latches_an_hour_before(self):
        sch = _schedule(_plan(14))
        sch.set_t3_peak(DAY, None)
        _service(_at(9, 0), sch, workday=False, soc=10.0, rce=HOLIDAY_RCE)
        plan = sch.t3_peak_for(DAY)
        assert plan is not None and plan.day_off
        assert (plan.sell_hour, plan.charge_to, plan.target) == (15, 68, 10)
        assert not sch.t3_peak_latched(DAY)
        _service(_at(14, 0), sch, workday=False, soc=12.0, rce=HOLIDAY_RCE)
        assert sch.t3_peak_latched(DAY)

    def test_a_day_off_after_its_dearest_hour_has_no_plan(self):
        sch = _schedule(_plan(14))
        sch.set_t3_peak(DAY, None)
        _service(_at(15, 20), sch, workday=False, soc=10.0, rce=HOLIDAY_RCE)
        assert sch.t3_peak_for(DAY) is None

    def test_a_cheap_day_off_has_no_plan(self):
        sch = _schedule(_plan(14))
        sch.set_t3_peak(DAY, None)
        flat = tuple(0.6 for _ in range(24))
        _service(_at(9, 0), sch, workday=False, soc=10.0, rce=flat)
        assert sch.t3_peak_for(DAY) is None

    def test_tomorrow_is_previewed_but_never_latched(self):
        sch = _schedule(_plan(14))
        sch.set_t3_peak(DAY, None)
        tomorrow = DAY + timedelta(days=1)
        _service(_at(21, 0), sch, tomorrow_rce=RCE)
        assert sch.t3_peak_for(tomorrow) is not None
        assert not sch.t3_peak_latched(tomorrow)

    def test_a_day_off_tomorrow_is_previewed_from_the_floor(self):
        sch = _schedule(_plan(14))
        tomorrow = DAY + timedelta(days=1)
        _service(_at(21, 0), sch, tomorrow_rce=HOLIDAY_RCE, workday_tomorrow=False)
        plan = sch.t3_peak_for(tomorrow)
        assert plan is not None and plan.day_off and plan.charge_to == 68


def test_a_hand_set_late_charge_start_does_not_break_the_schedule():
    sch = _schedule(_plan(14))
    late = sch._today[SlotKind.CHARGE_AFTERNOON].with_start(time(14, 30))
    sch._today[SlotKind.CHARGE_AFTERNOON] = late
    entry = sch._effective_entry(SlotKind.CHARGE_AFTERNOON, DAY, _at(13, 0))
    assert not entry.enabled
    assert _engaged(sch, _at(14, 0), 99.0) is SlotKind.DISCHARGE_T3_PEAK
