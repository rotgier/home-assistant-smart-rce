"""MorningChargeService — recomputes the weak-sun floor of the night charge.

Every minute, next to the morning discharge plan: gathers the day's Solcast p90,
the p40 consumption of earlier workdays, the day's prices, the export bar and
the T1 reserve without margin, and hands `BatterySchedule` a
`MorningChargeFloor` — or None while an input is missing, which leaves the
CHARGE_MORNING_AUTO slot on the reserve alone. Today's and tomorrow's floor,
the latter as soon as its reserve and prices exist (from 14:00), so the
evening already shows what the night will charge to. The rule itself lives in
the domain (`MorningChargeFloor.decide`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Final

from homeassistant.core import callback

from ..domain.battery_schedule import MorningChargeFloor, is_day_off

if TYPE_CHECKING:
    from ..domain.rce import RceDayPrices
    from .ems import Ems
    from .pv_surplus_service import PvSurplusService


class MorningChargeService:
    """Application service. HASS-unaware apart from the `callback` marker."""

    # Optimistic consumption: the 40th percentile of the earlier workdays.
    _CONSUMPTION_FRACTION: Final[float] = 0.4

    def __init__(
        self,
        ems: Ems,
        pv_surplus: PvSurplusService,
        clock: Callable[[], datetime],
    ) -> None:
        self._ems = ems
        self._pv_surplus = pv_surplus
        self._clock = clock

    @callback
    def refresh(self) -> None:
        """Rebuild today's and tomorrow's floor and hand them to the schedule."""
        now = self._clock()
        for day in (now.date(), now.date() + timedelta(days=1)):
            self._ems.battery_schedule_service.set_morning_charge_floor(
                day, self._floor(now, day)
            )

    def _floor(self, now: datetime, day: date) -> MorningChargeFloor | None:
        """`day`'s floor, None on a day off or while an input is missing."""
        is_workday = (
            self._ems.is_workday_tomorrow
            if day != now.date()
            else self._ems.is_workday_today
        )
        if is_day_off(day, is_workday):
            return None
        reserve = self._ems.battery_schedule_service.t1_reserve_for(day)
        prices = self._prices_for(day)
        consumption = self._pv_surplus.consumption_percentile(
            day, self._CONSUMPTION_FRACTION
        )
        pv = self._pv_surplus.p90_by_slot(day)
        if reserve is None or prices is None or consumption is None or not pv:
            return None
        return MorningChargeFloor.decide(
            day,
            pv=pv,
            consumption=consumption,
            prices=prices,
            export_threshold=self._ems.battery_schedule_service.export_threshold,
            reserve_without_margin=(
                reserve.value
                if reserve.value_without_margin is None
                else reserve.value_without_margin
            ),
        )

    def _prices_for(self, day: date) -> RceDayPrices | None:
        rce = self._ems.rce_prices.rce_prices
        if rce is None:
            return None
        prices = next(
            (p for p in (rce.today, rce.tomorrow) if p is not None and p.day == day),
            None,
        )
        # Positional hour index: a 23/25-hour DST day does not line up with the
        # clock, so the floor stays off then.
        return prices if prices is not None and len(prices.hour_price) == 24 else None
