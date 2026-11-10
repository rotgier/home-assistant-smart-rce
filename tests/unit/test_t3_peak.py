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

    def test_no_sale_when_t2_pays_more(self):
        rce = list(RCE)
        rce[17] = 4.60  # an evening hour dearer than 13 and 14
        evaluation = _evaluate(88.0, rce=tuple(rce))
        assert evaluation.plan is None
        assert not any(v.dearer_than_t2 for v in evaluation.variants)

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


class _Surplus:
    def live_soft_profile(self, day, *, tomorrow: bool):
        from custom_components.smart_rce.domain.pv_surplus import SurplusProfile

        return SurplusProfile.build(day, {}, None)  # no sun, flat 0.9 kWh/h


def _service(now: datetime, schedule, *, workday=True, soc=88.0):
    from types import SimpleNamespace

    from custom_components.smart_rce.application.t3_peak_service import T3PeakService
    from custom_components.smart_rce.domain.rce import RceDayPrices

    service = SimpleNamespace(
        slot=lambda scope, kind: schedule._today[kind],
        set_t3_peak=schedule.set_t3_peak,
        t3_peak_latched=schedule.t3_peak_latched,
        latch_t3_peak=schedule.latch_t3_peak,
        set_t3_peak_report=lambda day, evaluation, why: None,
    )
    net = tuple(p / 1.23 * 1000 for p in RCE)
    ems = SimpleNamespace(
        battery_schedule_service=service,
        is_workday_today=workday,
        last_input_state=SimpleNamespace(battery_soc=soc),
        rce_prices=SimpleNamespace(
            rce_prices=SimpleNamespace(today=RceDayPrices(None, DAY, net))
        ),
    )
    T3PeakService(ems, _Surplus(), lambda: now).refresh()


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

    def test_a_day_off_has_no_plan(self):
        sch = _schedule(_plan(14))
        sch.set_t3_peak(DAY, None)
        _service(_at(9, 0), sch, workday=False)
        assert sch.t3_peak_for(DAY) is None


def test_a_hand_set_late_charge_start_does_not_break_the_schedule():
    sch = _schedule(_plan(14))
    late = sch._today[SlotKind.CHARGE_AFTERNOON].with_start(time(14, 30))
    sch._today[SlotKind.CHARGE_AFTERNOON] = late
    entry = sch._effective_entry(SlotKind.CHARGE_AFTERNOON, DAY, _at(13, 0))
    assert not entry.enabled
    assert _engaged(sch, _at(14, 0), 99.0) is SlotKind.DISCHARGE_T3_PEAK
