"""PvChargeService — the PV charge on the price chart: what happened, what is coming.

Every minute: records today's run from the SoC reading (`BatteryChargeService`
owns and persists it) and rebuilds the outlook of today and of tomorrow. The
PV charge sensors show the plan day's (`BatteryScheduleService.plan_day`: today,
or tomorrow once the evening switched over); the day sensor picks either.
The forecast walks `charge_milestones` on Live Soft (p60 consumption) from:

- a charge under way (today, from its start on): now, at the current SoC;
- otherwise the day's start, at the level the slots before it leave the
  battery at — the last auto run ending by then, else the open session's
  target, else the SoC now.

A read model only; nothing steers on it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, callback

from ..domain.charge_start_shift import charge_milestones, to_minutes
from ..domain.pv_charge import PvChargeForecast, PvChargeOutlook
from ..domain.pv_surplus import surplus_window_end

if TYPE_CHECKING:
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
        start = self._start(day, today=today)
        if start is None or soc is None or now_min >= window_end:
            return None
        if today and (started or now_min >= start):
            begin, begin_soc = now_min, soc
        else:
            begin, begin_soc = start, self._soc_at(day, start, soc, today=today)
        profile = self._pv_surplus.live_soft_profile(day, tomorrow=not today)
        if profile is None:
            return None
        return PvChargeForecast(
            begin,
            begin_soc,
            charge_milestones(profile, begin, begin_soc, window_end),
            from_now=begin == now_min,
        )

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
