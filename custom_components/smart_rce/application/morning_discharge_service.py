"""MorningDischargeService — recomputes the automatic morning discharge.

Every minute, like the target of the auto morning charge: gathers the plan
day's prices, S0, the T1 reserve (a day off: the 10% floor) and the PV surplus
(Live Soft, p60 consumption of earlier days of the same kind) and hands
`BatterySchedule` a `MorningDischargePlan` — or None, which keeps the
DISCHARGE_MORNING_AUTO slot still. The plan day is today, or tomorrow once the
evening switched over (the evening preview, user's wish 17.10.2026). The rule itself lives in the domain (`MorningDischargePlan.decide`,
`lowest_soc_the_sun_fills`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import callback

from ..domain.battery_schedule import MorningDischargePlan, SlotKind, is_day_off
from ..domain.charge_start_shift import (
    full_charge_minute,
    lowest_soc_the_sun_fills,
    to_minutes,
)
from ..domain.pv_forecast import PvForecast
from ..domain.pv_surplus import surplus_window_end
from ..domain.target_soc import MIN_SOC_PERCENT

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
        """Plan for the plan day: today, or tomorrow once the evening switched over.

        Tomorrow's plan is the evening preview — it takes over at midnight and
        is recomputed every minute like today's, so it follows the forecast.
        """
        schedule = self._ems.battery_schedule_service
        tomorrow = schedule.plans_tomorrow_at(now)
        day = now.date() + timedelta(days=1) if tomorrow else now.date()
        is_workday = (
            self._ems.is_workday_tomorrow if tomorrow else self._ems.is_workday_today
        )
        entry = schedule.slot("today", SlotKind.DISCHARGE_MORNING_AUTO)
        # Computed with the slot switched off too — the sensor then shows what
        # it would do; the schedule runs a plan only for an enabled slot.
        if not SlotKind.DISCHARGE_MORNING_AUTO.profile.runs_on(day, is_workday):
            return None
        day_off = is_day_off(day, is_workday)
        prices = self._prices_for(day)
        s0 = (
            self._ems.charge_slots.tomorrow_start
            if tomorrow
            else self._ems.charge_slots.today_start
        )
        reserve = schedule.t1_reserve
        if prices is None or s0 is None:
            return None
        if not day_off and (reserve is None or reserve.day != day):
            return None
        # A day off searches from 05:00 whatever the slot's start says: the
        # slot's window is tuned for workdays.
        first = (
            MorningDischargePlan.EARLIEST_HOUR_DAY_OFF
            if day_off
            else self._first_hour(entry.start.hour, entry.start.minute)
        )
        best = MorningDischargePlan.best_hour(
            prices, first, min(entry.end.hour, s0.hour), day_off=day_off
        )
        if best is None:
            return None
        source = PvForecast.TOMORROW_LIVE_SOFT if tomorrow else PvForecast.LIVE_SOFT
        profile = self._pv_surplus.profile_for(
            day, history=True, pv_scale=1.0, source=source
        ) or self._pv_surplus.profile_for(
            day, history=False, pv_scale=1.0, source=source
        )
        if profile is None:
            return None
        t2 = surplus_window_end(day)
        # Starts from the reading without margin — the lowest the battery can
        # be left at, so the latest full charge (the cautious side).
        start_soc = (
            MIN_SOC_PERCENT
            if day_off or reserve is None
            else (
                reserve.value_without_margin
                if reserve.value_without_margin is not None
                else reserve.value
            )
        )
        full = full_charge_minute(profile, to_minutes(s0), start_soc, t2 * 60)
        # The sun charges the battery until the hour it fills in (inclusive).
        refill_end = t2 if full is None else min(t2, -(-full // 60))
        hours = MorningDischargePlan.hours_from(prices, best, s0.hour, refill_end)
        # The hour running now or the next one still to come (tomorrow: the first).
        hour = next((h for h in hours if tomorrow or h >= now.hour), None)
        if hour is None:
            return None
        # The line the hour stands on: the margin step at the hour's start,
        # not now's (at 07:41 the plan for 08:00 already takes the 08 step).
        # A day off has no T1 — the floor.
        line = (
            MIN_SOC_PERCENT
            if day_off or reserve is None
            else reserve.value_at(datetime.combine(day, time(hour), tzinfo=now.tzinfo))
        )
        fill = lowest_soc_the_sun_fills(
            profile, prices.hour_price, (hour + 1) * 60, to_minutes(s0), line
        )
        return MorningDischargePlan.decide(
            day=day,
            hour=hour,
            gross_price=prices.gross_at(hour),
            reserve=line,
            fill=fill,
            hours=hours,
            full_at_minute=full,
            day_off=day_off,
        )

    @staticmethod
    def _first_hour(hour: int, minute: int) -> int:
        """First whole hour inside the slot's window."""
        return hour + (1 if minute else 0)

    def _prices_for(self, day: date) -> RceDayPrices | None:
        rce = self._ems.rce_prices.rce_prices
        if rce is None:
            return None
        prices = next(
            (p for p in (rce.today, rce.tomorrow) if p is not None and p.day == day),
            None,
        )
        # Positional hour index: a 23/25-hour DST day does not line up with the
        # clock, so the plan stays off then.
        return prices if prices is not None and len(prices.hour_price) == 24 else None
