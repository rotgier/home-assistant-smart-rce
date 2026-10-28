"""Weak-sun floor of the night charge (CHARGE_MORNING_AUTO), decision 22.10.2026."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

from custom_components.smart_rce.application.morning_charge_service import (
    MorningChargeService,
)
from custom_components.smart_rce.domain.battery_schedule import (
    BatterySchedule,
    BatteryScheduleEntry,
    MorningChargeFloor,
    ReserveWindow,
    SlotEngaged,
    SlotKind,
    T1SocReserve,
)
from custom_components.smart_rce.domain.charge_curve import pp_below_full
from custom_components.smart_rce.domain.pv_surplus import slots_from_hourly
from custom_components.smart_rce.domain.rce import RceDayPrices

TZ = timezone.utc
DAY = date(2026, 10, 22)  # Thursday, winter: T2 from 16:00
NEXT = date(2026, 10, 23)
CHEAP = 400.0  # net, 492 gross: below the 750 bar
DEAR = 900.0  # net, 1107 gross
FLAT_CONS = dict.fromkeys(range(48), 0.2)  # kWh per 30-min slot


def _prices(day: date, net: float, **hours: float) -> RceDayPrices:
    prices = [net] * 24
    for hour, price in hours.items():
        prices[int(hour[1:])] = price
    return RceDayPrices(published_at=None, day=day, hour_price=tuple(prices))


def _decide(prices: RceDayPrices, pv_rate: float, without_margin: int = 50):
    return MorningChargeFloor.decide(
        DAY,
        pv=slots_from_hourly(dict.fromkeys(range(9, 16), pv_rate)),
        consumption=FLAT_CONS,
        prices=prices,
        export_threshold=750.0,
        reserve_without_margin=without_margin,
    )


class TestDecide:
    def test_a_dear_day_stores_no_sun_and_charges_to_full(self):
        # 28.10.2026: every hour 07-16 above the bar, so no surplus counts and
        # the night charge goes to 100 (its sun is exported anyway).
        floor = _decide(_prices(DAY, DEAR), pv_rate=3.0)
        assert (floor.surplus_kwh, floor.fill, floor.dip) == (0.0, 100, 40)
        assert floor.dear_day
        assert floor.target == MorningChargeFloor.DEAR_DAY_CAP == 100
        assert floor.lifts(65) == 100
        assert floor.to_dict()["dear_day"] is True

    def test_one_cheap_hour_keeps_the_cap_of_90(self):
        floor = _decide(_prices(DAY, DEAR, h12=CHEAP), pv_rate=0.1)
        assert not floor.dear_day
        assert floor.target == MorningChargeFloor.CAP

    def test_a_sunny_cheap_day_leaves_the_reserve_alone(self):
        floor = _decide(_prices(DAY, CHEAP), pv_rate=3.0)
        assert floor.fill == 10
        assert floor.target is None
        assert floor.lifts(65) == 65

    def test_only_hours_below_the_bar_count(self):
        # Cheap 12:00 and 13:00 only: 2 h x 2 slots x (1.0 - 0.2) = 3.2 kWh.
        floor = _decide(_prices(DAY, DEAR, h12=CHEAP, h13=CHEAP), pv_rate=2.0)
        assert floor.surplus_kwh == 3.2
        assert floor.fill == round(100 - pp_below_full(3.2))
        assert floor.target == min(90, floor.fill + 40)

    def test_the_dip_comes_from_the_reserve_without_margin(self):
        floor = _decide(_prices(DAY, DEAR), pv_rate=3.0, without_margin=10)
        assert floor.dip == 0

    def test_the_floor_never_lowers_a_higher_reserve(self):
        floor = _decide(_prices(DAY, DEAR, h12=CHEAP, h13=CHEAP), pv_rate=2.0)
        assert floor.target is not None
        assert floor.lifts(floor.target + 5) == floor.target + 5


def _reserve(day: date, value: int = 65, without: int = 50) -> T1SocReserve:
    return T1SocReserve(
        day=day,
        value=value,
        flat=value,
        prev_without_worst=None,
        dip_kwh=None,
        source="live_soft",
        window=ReserveWindow.FULL,
        margin_pp=value - without,
        value_without_margin=without,
    )


class TestSchedule:
    def _engaged(self, floor: MorningChargeFloor | None) -> SlotEngaged:
        today = BatteryScheduleEntry.defaults_for_all_kinds()
        today[SlotKind.CHARGE_MORNING_AUTO] = BatteryScheduleEntry.default_for(
            SlotKind.CHARGE_MORNING_AUTO, enabled=True
        )
        sch = BatterySchedule(today=today)
        now = datetime(2026, 10, 22, 6, 50, tzinfo=TZ)
        sch.set_t1_reserve(_reserve(DAY), now)
        sch.set_morning_charge_floor(DAY, floor)
        _, events = sch.compute_operation(now, 50.0, is_workday=True)
        (engaged,) = [e for e in events if isinstance(e, SlotEngaged)]
        assert engaged.slot is SlotKind.CHARGE_MORNING_AUTO
        return engaged

    def test_the_night_charge_goes_to_the_floor_when_higher(self):
        engaged = self._engaged(_decide(_prices(DAY, DEAR), pv_rate=3.0))
        assert engaged.target_soc == 100.0
        assert "weak-sun floor 100" in (engaged.target_source or "")
        assert "dear day" in (engaged.target_source or "")

    def test_without_a_floor_the_reserve_stands(self):
        engaged = self._engaged(None)
        assert engaged.target_soc == 65.0
        assert engaged.target_source == "live_soft"

    def test_a_floor_that_does_not_bind_says_so(self):
        engaged = self._engaged(_decide(_prices(DAY, CHEAP), pv_rate=3.0))
        assert engaged.target_soc == 65.0
        assert "weak-sun floor none" in (engaged.target_source or "")


def _service(*, consumption=FLAT_CONS, workday=True, reserves=None):
    floors: dict = {}
    reserves = {DAY: _reserve(DAY)} if reserves is None else reserves
    ems = SimpleNamespace(
        battery_schedule_service=SimpleNamespace(
            t1_reserve_for=reserves.get,
            export_threshold=750.0,
            set_morning_charge_floor=lambda day, floor: floors.__setitem__(day, floor),
        ),
        is_workday_today=workday,
        is_workday_tomorrow=True,
        rce_prices=SimpleNamespace(
            rce_prices=SimpleNamespace(
                today=_prices(DAY, DEAR), tomorrow=_prices(NEXT, CHEAP)
            )
        ),
    )
    surplus = SimpleNamespace(
        consumption_percentile=lambda day, fraction: consumption,
        p90_by_slot=lambda day: slots_from_hourly(dict.fromkeys(range(9, 16), 3.0)),
    )
    service = MorningChargeService(
        ems, surplus, lambda: datetime(2026, 10, 22, 4, 30, tzinfo=TZ)
    )
    service.refresh()
    return floors


class TestService:
    def test_today_and_tomorrow_get_their_floor(self):
        floors = _service(reserves={DAY: _reserve(DAY), NEXT: _reserve(NEXT)})
        assert floors[DAY].target == 100  # every hour above the bar
        assert floors[NEXT].target is None  # cheap and sunny

    def test_missing_inputs_leave_no_floor(self):
        assert _service(consumption=None)[DAY] is None
        assert _service()[NEXT] is None  # no reserve for tomorrow yet

    def test_a_day_off_has_no_floor(self):
        assert _service(workday=False)[DAY] is None
