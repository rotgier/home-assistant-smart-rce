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
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change

from ..domain.evening_plan import EveningPlan
from .evening_plan_notifier import notify_evening_plan, notify_evening_plan_failed

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from ..application.ems import Ems

_LOGGER = logging.getLogger(__name__)

# Attempts, earliest first. 21:05 is the winter switchover (T2 ends at 21:00);
# 22:05 the summer one; 23:05 covers days off, whose window runs to 23:00. The
# later hours also serve as retries when an evening is still discharging.
# Which day each attempt plans follows `EveningPlan.plans_tomorrow_at`, so an
# attempt that fires before today's window closes simply re-plans today.
_EVENING_RUN_HOURS: Final[tuple[int, ...]] = (21, 22, 23, 0)
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
        self._failure_reported: bool = False

    def start(self) -> CALLBACK_TYPE:
        """Subscribe both runs. Returns an unsubscribe for `async_on_unload`."""
        self._cancels = [
            async_track_time_change(
                self._hass,
                self._guarded(self._run_evening, "tura wieczorna"),
                hour=list(_EVENING_RUN_HOURS),
                minute=_EVENING_RUN_MINUTE,
                second=0,
            ),
            async_track_time_change(
                self._hass,
                self._guarded(self._run_afternoon, "przegląd popołudniowy"),
                hour=_AFTERNOON_HOURS,
                minute=_AFTERNOON_MINUTES,
                second=30,
            ),
        ]
        return self._stop

    def _guarded(
        self,
        run: Callable[[datetime], Coroutine[Any, Any, None]],
        label: str,
    ) -> Callable[[datetime], Coroutine[Any, Any, None]]:
        """Wrap a run so a crash is reported instead of vanishing into the log.

        Both runs are fired by `async_track_time_change`, which hands the
        coroutine to the event loop and never looks at it again — so anything
        raised ends up as "Task exception was never retrieved" and the evening
        quietly keeps yesterday's windows.

        Only the first failure of an outage is announced. The afternoon sweep
        fires every five minutes; left unguarded, one broken deploy would send
        a dozen messages an hour and teach the reader to mute the channel. The
        flag clears on the next clean run, so a fresh outage speaks up again.
        """

        async def guarded(now: datetime) -> None:
            try:
                await run(now)
            except Exception as err:  # noqa: BLE001 - a run must not die silently
                _LOGGER.exception("%s failed", label)
                if not self._failure_reported:
                    self._failure_reported = True
                    await notify_evening_plan_failed(self._hass, run=label, error=err)
            else:
                self._failure_reported = False

        return guarded

    @callback
    def _stop(self) -> None:
        for cancel in self._cancels:
            cancel()
        self._cancels = []

    async def _run_evening(self, now: datetime) -> None:
        """Plan tomorrow evening, unless tonight is still discharging."""
        target = self._evening_target(now)
        # Logged on entry, not only on success: when a run does nothing, the
        # reason is the thing worth knowing, and a silent skip left one
        # September evening unexplained after the buffer rolled over.
        _LOGGER.info("Evening run at %s — target %s", now.strftime("%H:%M"), target)
        if self._planned_evening_on == target:
            _LOGGER.info("Evening run skipped — %s already planned", target)
            return
        if self._ems.battery_schedule_service.is_discharging_now:
            _LOGGER.info("Evening plan deferred — a discharge is still running")
            return
        # Past midnight the target evening is already today, so the plan must
        # be read off today's prices rather than tomorrow's.
        if await self._plan_and_apply(
            now,
            for_tomorrow=EveningPlan.plans_tomorrow_at(
                now, is_workday=self._ems.is_workday_today
            ),
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
        application = await self._ems.battery_schedule_service.adopt_evening_plan(
            plan, now
        )
        await notify_evening_plan(
            self._hass, plan, application=application, for_tomorrow=for_tomorrow
        )
        return True

    def _evening_target(self, now: datetime) -> date:
        """Date of the evening this run is planning.

        An attempt fired after today's window closes plans the next day; one
        fired before it (or the 00:05 retry) plans today. Naming the target
        rather than the run lets every attempt share one "done" marker.
        """
        if EveningPlan.plans_tomorrow_at(now, is_workday=self._ems.is_workday_today):
            return now.date() + timedelta(days=1)
        return now.date()
