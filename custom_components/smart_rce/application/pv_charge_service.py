"""PvChargeService — the PV charge on the price chart: what happened, what is coming.

Every minute: records today's run from the SoC reading (`BatteryChargeService`
owns and persists it) and rebuilds the outlook of today and of tomorrow. The
PV charge sensors show the plan day's (`BatteryScheduleService.plan_day`: today,
or tomorrow once the evening switched over); the day sensor picks either.
The forecast walks `charge_milestones` on Live Soft (p60 consumption) from:

- a charge under way (today, from its start on): now, at the current SoC;
- otherwise the start the forecast points to now (`find_candidate_at`, the
  rule of the forecast shift, recomputed every minute), at the T1 reserve with
  the margin of that start's hour — the lowest the morning may leave the
  battery at (user, 18.10.2026); a day off without T1 from the 10% floor.
  Without prices or a reserve: the start in force, at the level the slots
  before it leave the battery at.

The applied start (`ChargeStartShift`, guarded, from the current SoC) may
still differ; the outlook shows where the forecast points, not the decision.

A read model only; nothing steers on it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, callback

from ..domain.battery_schedule import is_day_off
from ..domain.charge_curve import kwh_to_full
from ..domain.charge_start_shift import (
    charge_milestones,
    find_candidate_at,
    to_minutes,
    to_time,
)
from ..domain.pv_charge import PvChargeForecast, PvChargeOutlook
from ..domain.pv_surplus import surplus_window_end
from ..domain.target_soc import MIN_SOC_PERCENT

if TYPE_CHECKING:
    from ..domain.pv_surplus import SurplusProfile
    from .ems import Ems
    from .pv_surplus_service import PvSurplusService


class PvChargeService:
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
        self._listeners: dict[CALLBACK_TYPE, CALLBACK_TYPE] = {}
        self._outlooks: dict[date, PvChargeOutlook] = {}

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        def remove_listener() -> None:
            self._listeners.pop(remove_listener)

        self._listeners[remove_listener] = update_callback
        return remove_listener

    @property
    def outlook(self) -> PvChargeOutlook | None:
        """The plan day's outlook — today, or tomorrow once the evening switched."""
        return self.outlook_on(self._ems.battery_schedule_service.plan_day)

    def outlook_on(self, day: date) -> PvChargeOutlook | None:
        """Today's or tomorrow's outlook; None for any other day."""
        return self._outlooks.get(day)

    @callback
    def refresh(self) -> None:
        """Record today's reading, rebuild today's and tomorrow's outlook, notify."""
        now = self._clock()
        state = self._ems.last_input_state
        soc = state.battery_soc if state is not None else None
        if soc is not None:
            self._ems.battery_charge_service.observe_pv_charge(
                now, soc, surplus_window_end(now.date()) * 60
            )
        today = now.date()
        self._outlooks = {
            day: self._outlook(now, soc, day)
            for day in (today, today + timedelta(days=1))
        }
        for update_callback in list(self._listeners.values()):
            update_callback()

    def _outlook(self, now: datetime, soc: float | None, day: date) -> PvChargeOutlook:
        today = day == now.date()
        window_end = surplus_window_end(day) * 60
        run = self._ems.battery_charge_service.pv_charge_run if today else None
        if run is not None and run.day != day:
            run = None
        # Before the day (tomorrow's preview) nothing has happened: the outlook
        # runs from the start of the day.
        now_min = to_minutes(now.time()) if today else 0
        return PvChargeOutlook.build(
            day,
            run=run,
            forecast=self._forecast(
                day,
                now_min,
                soc,
                window_end,
                today=today,
                started=run is not None and run.start is not None,
            ),
            now=now_min,
            soc=soc,
            window_end=window_end,
        )

    def _forecast(
        self,
        day: date,
        now_min: int,
        soc: float | None,
        window_end: int,
        *,
        today: bool,
        started: bool,
    ) -> PvChargeForecast | None:
        if soc is None or now_min >= window_end:
            return None
        profile = self._pv_surplus.live_soft_profile(day, tomorrow=not today)
        if profile is None:
            return None
        if started:
            begin, begin_soc = now_min, soc
        else:
            start = self._forecast_start(day, now_min, soc, profile, today)
            if start is None:
                return None
            if today and now_min >= start:
                begin, begin_soc = now_min, soc
            else:
                begin, begin_soc = start, self._start_soc(day, start, soc, today=today)
        return PvChargeForecast(
            begin,
            begin_soc,
            charge_milestones(profile, begin, begin_soc, window_end),
            from_now=begin == now_min,
        )

    def _forecast_start(
        self,
        day: date,
        now_min: int,
        soc: float,
        profile: SurplusProfile,
        today: bool,
    ) -> int | None:
        """Start the forecast points to now; the start in force without prices."""
        slots = self._ems.charge_slots
        s0 = slots.today_start if today else slots.tomorrow_start
        prices = self._prices(day)
        if s0 is None or prices is None:
            return self._start(day, today=today)
        return find_candidate_at(
            profile,
            prices,
            now_min,
            to_minutes(s0),
            lambda begin: kwh_to_full(self._start_soc(day, begin, soc, today=today)),
        ).start

    def _prices(self, day: date) -> tuple[float, ...] | None:
        rce = self._ems.rce_prices.rce_prices
        if rce is None:
            return None
        prices = next(
            (p for p in (rce.today, rce.tomorrow) if p is not None and p.day == day),
            None,
        )
        # Positional hour index: a 23/25-hour DST day does not line up.
        if prices is None or len(prices.hour_price) != 24:
            return None
        return tuple(prices.hour_price)

    def _start_soc(self, day: date, start: int, soc: float, *, today: bool) -> float:
        """Level the battery starts taking PV at: the T1 reserve at the start's hour.

        The margin is the step in force at the start (08:00 more than later);
        a day off has no T1 — the 10% floor. Without a reserve for the day,
        the level the slots before the start leave the battery at.
        """
        is_workday = (
            self._ems.is_workday_today if today else self._ems.is_workday_tomorrow
        )
        if is_day_off(day, is_workday):
            return float(MIN_SOC_PERCENT)
        reserve = self._ems.battery_schedule_service.t1_reserve_for(day)
        if reserve is None:
            return self._soc_at(day, start, soc, today=today)
        at = datetime.combine(day, to_time(start), tzinfo=self._clock().tzinfo)
        return float(reserve.value_at(at))

    def _start(self, day: date, *, today: bool) -> int | None:
        """Return the day's charge start: today the one in force, tomorrow plan or S0."""
        charge = self._ems.battery_charge_service
        if today:
            start = charge.start_charge_hour_override
        else:
            plan = charge.tomorrow_plan
            start = (
                plan.value
                if plan is not None and plan.day == day
                else self._ems.charge_slots.tomorrow_start
            )
        return to_minutes(start) if start is not None else None

    def _soc_at(self, day: date, start: int, soc: float, *, today: bool) -> float:
        """Level the slots before the start leave the battery at."""
        schedule = self._ems.battery_schedule_service
        level = soc
        if today:
            level = next(
                (
                    s.target_soc
                    for s in schedule.sessions_today
                    if s.is_open and s.target_soc is not None
                ),
                level,
            )
        for run in schedule.upcoming_runs_on(day):
            if run.end.date() == day and to_minutes(run.end.time()) <= start:
                level = run.target_soc
        return level
