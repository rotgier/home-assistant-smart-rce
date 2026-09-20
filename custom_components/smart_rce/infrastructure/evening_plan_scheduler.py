"""Runs the evening discharge planner twice a day and reports what it did.

Two runs, planning the same evening from different vantage points:

- **22:05** — plans tomorrow evening from prices published that afternoon.
  Deferred to 23:05 and then 00:05 while a discharge is still running, because
  rewriting a slot mid-engagement would cut it short.
- **from 15:00, every five minutes** — replans the evening now under way, this
  time with tomorrow's morning prices available to veto it. Stops for the day
  once those prices are in and the plan has been applied.

Both runs report through `script.notify_text`, so the plan lands in Telegram
without a voice call.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Final

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change

from ..domain.evening_plan import EveningPlan
from .evening_plan_notifier import notify_evening_plan

if TYPE_CHECKING:
    from ..application.ems import Ems

_LOGGER = logging.getLogger(__name__)

# 22:05 first; the later hours are retries for an evening still discharging.
_EVENING_RUN_HOURS: Final[tuple[int, ...]] = (22, 23, 0)
_EVENING_RUN_MINUTE: Final[int] = 5

# The afternoon sweep polls until tomorrow's prices show up (published ~14:00,
# but late often enough to be worth retrying rather than firing once).
_AFTERNOON_HOURS: Final[list[int]] = list(range(15, 22))
_AFTERNOON_MINUTES: Final[list[int]] = list(range(0, 60, 5))


class EveningPlanScheduler:
    """Owns the timing of the planner; the planning itself lives in the domain."""

    def __init__(self, hass: HomeAssistant, ems: Ems) -> None:
        self._hass = hass
        self._ems = ems
        self._planned_evening_on: date | None = None
        self._revised_evening_on: date | None = None
        self._cancels: list[CALLBACK_TYPE] = []

    def start(self) -> CALLBACK_TYPE:
        """Subscribe both runs. Returns an unsubscribe for `async_on_unload`."""
        self._cancels = [
            async_track_time_change(
                self._hass,
                self._run_evening,
                hour=list(_EVENING_RUN_HOURS),
                minute=_EVENING_RUN_MINUTE,
                second=0,
            ),
            async_track_time_change(
                self._hass,
                self._run_afternoon,
                hour=_AFTERNOON_HOURS,
                minute=_AFTERNOON_MINUTES,
                second=30,
            ),
        ]
        return self._stop

    @callback
    def _stop(self) -> None:
        for cancel in self._cancels:
            cancel()
        self._cancels = []

    async def _run_evening(self, now: datetime) -> None:
        """Plan tomorrow evening, unless tonight is still discharging."""
        target = self._evening_target(now)
        if self._planned_evening_on == target:
            return  # already done for that evening; later hours are retries
        if self._ems.battery_schedule_service.is_discharging_now:
            _LOGGER.info("Evening plan deferred — a discharge is still running")
            return
        # Past midnight the target evening is already today, so the plan must
        # be read off today's prices rather than tomorrow's.
        if await self._plan_and_apply(
            now, for_tomorrow=EveningPlan.plans_tomorrow_at(now)
        ):
            self._planned_evening_on = target

    async def _run_afternoon(self, now: datetime) -> None:
        """Re-plan this evening once tomorrow's prices allow the morning veto."""
        if self._revised_evening_on == now.date():
            return
        if not self._ems.rce_prices.has_tomorrow:
            return  # keep polling; prices are late
        if await self._plan_and_apply(now, for_tomorrow=False):
            self._revised_evening_on = now.date()

    async def _plan_and_apply(self, now: datetime, *, for_tomorrow: bool) -> bool:
        """Build the plan, write it to the slots, report. False = try again later."""
        plan = self._ems.build_evening_plan(for_tomorrow=for_tomorrow)
        if plan is None:
            _LOGGER.debug("Evening plan unavailable (prices or calendar missing)")
            return False
        applied = await self._ems.battery_schedule_service.adopt_evening_plan(plan, now)
        await notify_evening_plan(
            self._hass, plan, applied=applied, for_tomorrow=for_tomorrow
        )
        return True

    @staticmethod
    def _evening_target(now: datetime) -> date:
        """Date of the evening this run is planning.

        22:05 and 23:05 plan the NEXT day's evening; the 00:05 retry already
        sits inside that day. Naming the target rather than the run lets all
        three fire against one "done" marker.
        """
        if now.hour >= _EVENING_RUN_HOURS[0]:
            return now.date() + timedelta(days=1)
        return now.date()
