"""MorningDischargeService — recomputes today's automatic morning discharge.

Every minute, like the target of the auto morning charge: gathers today's
prices, S0, the T1 reserve and the PV surplus (Live Soft, p60
consumption of earlier workdays) and hands `BatterySchedule` a
`MorningDischargePlan` — or None, which keeps the DISCHARGE_MORNING_AUTO slot
still. The rule itself lives in the domain (`MorningDischargePlan.decide`,
`lowest_soc_the_sun_fills`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.core import callback

from ..domain.battery_schedule import MorningDischargePlan, SlotKind
from ..domain.charge_start_shift import lowest_soc_the_sun_fills, to_minutes
from ..domain.pv_surplus import surplus_window_end

if TYPE_CHECKING:
    from ..domain.rce import RceDayPrices
    from .ems import Ems
    from .pv_surplus_service import PvSurplusService


class MorningDischargeService:
    """Application service. HASS-unaware apart from the `callback` marker."""

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
        """Rebuild today's plan and hand it to the schedule."""
        self._ems.battery_schedule_service.set_morning_discharge(
            self._plan(self._clock())
        )

    def _plan(self, now: datetime) -> MorningDischargePlan | None:
        schedule = self._ems.battery_schedule_service
        entry = schedule.slot("today", SlotKind.DISCHARGE_MORNING_AUTO)
        # Computed with the slot switched off too — the sensor then shows what
        # it would do; the schedule runs a plan only for an enabled slot.
        if not self._ems.is_workday_today:
            return None
        prices = self._today_prices(now)
        s0 = self._ems.charge_slots.today_start
        reserve = schedule.t1_reserve
        if prices is None or s0 is None or reserve is None or reserve.day != now.date():
            return None
        best = MorningDischargePlan.best_hour(
            prices,
            self._first_hour(entry.start.hour, entry.start.minute),
            min(entry.end.hour, s0.hour),
        )
        if best is None:
            return None
        hours = MorningDischargePlan.hours_from(
            prices, best, s0.hour, surplus_window_end(now.date())
        )
        # The hour running now or the next one still to come.
        hour = next((h for h in hours if h >= now.hour), None)
        if hour is None:
            return None
        profile = self._pv_surplus.today_profile(history=True, pv_scale=1.0)
        if profile is None:
            profile = self._pv_surplus.today_profile(history=False, pv_scale=1.0)
        if profile is None:
            return None
        fill = lowest_soc_the_sun_fills(
            profile, prices.hour_price, (hour + 1) * 60, to_minutes(s0), reserve.value
        )
        return MorningDischargePlan.decide(
            day=now.date(),
            hour=hour,
            gross_price=prices.gross_at(hour),
            reserve=reserve.value,
            fill=fill,
            hours=hours,
        )

    @staticmethod
    def _first_hour(hour: int, minute: int) -> int:
        """First whole hour inside the slot's window."""
        return hour + (1 if minute else 0)

    def _today_prices(self, now: datetime) -> RceDayPrices | None:
        rce = self._ems.rce_prices.rce_prices
        if rce is None or rce.today is None or rce.today.day != now.date():
            return None
        # Positional hour index: a 23/25-hour DST day does not line up with the
        # clock, so the plan stays off then.
        return rce.today if len(rce.today.hour_price) == 24 else None
