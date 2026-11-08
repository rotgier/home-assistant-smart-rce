"""DearDayChoice — a dear workday kept full or run as a normal one (08.11.2026)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from custom_components.smart_rce.domain.battery_schedule import (
    BatterySchedule,
    BatteryScheduleEntry,
    MorningChargeFloor,
    ReserveWindow,
    SlotKind,
    T1SocReserve,
)
from custom_components.smart_rce.domain.dear_day_choice import (
    MIN_ADVANTAGE_ZL,
    DearDayChoice,
    DearDayHour,
    DearDayVariant,
)
import pytest

DAY = date(2026, 11, 9)
# RCE 09.11.2026 (net PLN/MWh) and the Live Soft / p60 forecast of 08.11 22:40.
RCE = (
    937.78, 900.62, 881.06, 871.15, 887.81, 1115.19, 1896.55, 1914.99,
    1862.33, 1281.86, 827.07, 685.38, 856.83, 1269.76, 2155.14, 3059.79,
    2989.97, 2795.39, 2433.13, 2382.82, 1945.37, 1539.9, 1286.7, 963.76,
)  # fmt: skip
PV = {7: 0.775, 8: 2.564, 9: 3.679, 10: 4.532, 11: 4.883, 12: 4.832, 13: 3.909,
      14: 2.574, 15: 0.744}  # fmt: skip
CONS = {4: 0.321, 5: 0.465, 6: 0.543, 7: 0.759, 8: 0.696, 9: 1.168, 10: 1.094,
        11: 0.751, 12: 0.855, 13: 1.679, 14: 0.843, 15: 0.845}  # fmt: skip
# G13 2026 gross (zl/kWh): energy + distribution, T1 07-13, T3 otherwise.
T1, T3 = (0.83, 0.33), (0.45, 0.18)


def _hours(pv_factor: float = 1.0) -> list[DearDayHour]:
    out = []
    for h in range(24):
        pv, cons = PV.get(h, 0.0) * pv_factor, CONS.get(h, 0.5)
        energy, distribution = T1 if 7 <= h < 13 else T3
        out.append(
            DearDayHour(
                surplus_kwh=max(0.0, pv - cons),
                deficit_kwh=max(0.0, cons - pv),
                rce_gross=RCE[h] * 1.23 / 1000,
                buy_energy=energy,
                buy_distribution=distribution,
            )
        )
    return out


def _choice(soc: float, *, pv_factor: float = 1.0, sale=(7, 8, 9)) -> DearDayChoice:
    return DearDayChoice.decide(
        DAY,
        _hours(pv_factor),
        soc=soc,
        night_start_hour=4,
        night_end_hour=7,
        reserve=37,
        s0_hour=10,
        t2_hour=16,
        sale_hours=sale,
        sale_target=37 if sale else None,
    )


class TestDecide:
    def test_a_full_battery_sells_in_the_dear_morning(self):
        # 09.11.2026: 07:00 at 2355 gross, the sun refills 10-12 at ~970.
        choice = _choice(99.0)
        assert choice.chosen is DearDayVariant.NORMAL
        assert choice.advantage_zl > 4.0
        assert choice.normal.sold_kwh > 5.0

    def test_an_empty_battery_charges_the_night_to_full(self):
        # The night's T3 energy covers T1 and the sun goes to the grid.
        choice = _choice(22.0)
        assert choice.chosen is DearDayVariant.FULL
        assert choice.normal.sold_kwh == 0.0

    def test_the_normal_night_never_buys_to_sell(self):
        # ADR-028 pt 3: the night charge stops at the reserve, so from an empty
        # battery the morning has nothing to sell.
        assert _choice(10.0).normal.sold_kwh == 0.0

    def test_a_weak_sun_tops_up_in_the_afternoon(self):
        # Half the sun: CHARGE_AFTERNOON buys the rest at T3 before T2.
        choice = _choice(99.0, pv_factor=0.5)
        assert choice.normal.top_up_kwh > choice.full.top_up_kwh

    def test_without_a_sale_the_normal_day_needs_the_bar(self):
        # A small edge is not enough: the forecast has its error.
        choice = _choice(60.0, sale=())
        assert choice.advantage_zl < MIN_ADVANTAGE_ZL
        assert choice.chosen is DearDayVariant.FULL

    def test_to_dict_lists_both_variants(self):
        d = _choice(99.0).to_dict()
        assert d["chosen"] == "normal"
        assert [v["key"] for v in d["variants"]] == ["full", "normal"]
        assert d["variants"][0]["night_target"] == 100

    def test_with_chosen_keeps_the_values(self):
        choice = _choice(99.0)
        latched = choice.with_chosen(DearDayVariant.FULL)
        assert latched.chosen is DearDayVariant.FULL
        assert latched.advantage_zl == choice.advantage_zl


def _reserve() -> T1SocReserve:
    return T1SocReserve(
        day=DAY,
        value=37,
        flat=37,
        prev_without_worst=None,
        dip_kwh=None,
        source="live_soft",
        window=ReserveWindow.FULL,
    )


def _schedule() -> BatterySchedule:
    today = BatteryScheduleEntry.defaults_for_all_kinds()
    night = today[SlotKind.CHARGE_MORNING_AUTO]
    today[SlotKind.CHARGE_MORNING_AUTO] = night.with_enabled(True)
    sch = BatterySchedule(today=today)
    sch.set_t1_reserve(_reserve(), datetime(2026, 11, 8, 22, 0, tzinfo=UTC))
    dear = MorningChargeFloor(
        day=DAY, surplus_kwh=0.0, fill=100, dip=12, target=100, dear_day=True
    )
    sch.set_morning_charge_floor(DAY, dear)
    return sch


class TestSchedule:
    def test_a_dear_day_lifts_the_night_to_full(self):
        sch = _schedule()
        assert sch.night_floor_for(DAY) is not None
        entry = sch._effective_entry(SlotKind.CHARGE_MORNING_AUTO, DAY)
        assert entry.target_soc == 100

    def test_a_dear_day_run_as_normal_charges_to_the_reserve(self):
        sch = _schedule()
        sch.set_dear_day_choice(DAY, _choice(99.0))
        assert sch.night_floor_for(DAY) is None
        assert sch.morning_charge_floor_for(DAY) is not None  # raw, for the plan
        entry = sch._effective_entry(SlotKind.CHARGE_MORNING_AUTO, DAY)
        assert entry.target_soc == 37

    def test_the_latch_survives_a_restart(self):
        sch = _schedule()
        assert sch.latch_dear_day(DAY, "full")
        assert not sch.latch_dear_day(DAY, "full")
        restored = BatterySchedule.from_dict(sch.to_dict())
        assert restored.dear_day_latched(DAY) == "full"
        assert restored.dear_day_latched(date(2026, 11, 10)) is None

    @pytest.mark.parametrize("raw", [None, ["bad"], ["2026-11-09"]])
    def test_a_broken_latch_is_dropped(self, raw):
        data = _schedule().to_dict() | {"dear_day_latch": raw}
        assert BatterySchedule.from_dict(data).dear_day_latched(DAY) is None
