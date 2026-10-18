"""BatteryDayService — the battery bands of the day picked on the dashboard.

Today and tomorrow are assembled from the live aggregates (sessions so far,
auto runs ahead, evening slots set to run, PV charge outlook); a past day is
rebuilt from the recorder once and kept — it no longer changes. Other days
(beyond tomorrow, or past what the recorder holds) come back empty.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from ..domain.battery_day import BatteryDay

if TYPE_CHECKING:
    from ..infrastructure.battery_day_history import BatteryDayHistory
    from .ems import Ems
    from .pv_charge_service import PvChargeService


class BatteryDayService:
    """Application service; live days synchronously, past days via the recorder."""

    def __init__(
        self,
        ems: Ems,
        pv_charge: PvChargeService,
        history: BatteryDayHistory,
        clock: Callable[[], datetime],
    ) -> None:
        self._ems = ems
        self._pv_charge = pv_charge
        self._history = history
        self._clock = clock
        self._past: dict[date, BatteryDay] = {}

    def is_live(self, day: date) -> bool:
        """Tell whether `day` follows the live aggregates (today or tomorrow)."""
        today = self._clock().date()
        return today <= day <= today + timedelta(days=1)

    async def async_day(self, day: date) -> BatteryDay:
        today = self._clock().date()
        if self.is_live(day):
            return self._live(day, today=day == today)
        if day > today:
            return BatteryDay(day, "none")
        cached = self._past.get(day)
        if cached is None:
            cached = self._past[day] = await self._history.async_day(day)
        return cached

    def _live(self, day: date, *, today: bool) -> BatteryDay:
        schedule = self._ems.battery_schedule_service
        return BatteryDay(
            day,
            "live",
            sessions=tuple(schedule.sessions_today) if today else (),
            upcoming=tuple(schedule.upcoming_runs_on(day)),
            evening=tuple(schedule.evening_windows_on(day)),
            pv=self._pv_charge.outlook_on(day),
        )
