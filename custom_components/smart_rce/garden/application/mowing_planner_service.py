"""MowingPlannerService — orchestrates the mowing planner (garden 2b).

Pulls planner inputs from injected ports on every `recompute()` (wired in the
factory to: Luba telemetry changes, forecast updates, non-work target changes
and a 1-minute tick — `should_start` compares `now` against `opt_start`, so
time itself is an input) and exposes the latest `PlannerDecision` to the
sensor entities. Notifies listeners only when the decision actually changed,
so the minutely tick is free while nothing moves.

No hass and no entity ids here: telemetry comes from `LubaStateReader`,
forecast from the ems-published `HourlyForecastProvider` Protocol (cross-context
port, see `application/hourly_forecast.py`), quiet hours from `NonWorkService`
(the HA-owned target; falls back to the cloud sensor via `NonWorkReader` with a
warning while the target is unset).
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Final

from custom_components.smart_rce.garden.domain.mowing_planner import (
    MowingInput,
    MowingPlanner,
    PlannerDecision,
)
from custom_components.smart_rce.garden.infrastructure.forecast_reader import (
    parse_forecast_slots,
)
from homeassistant.core import callback

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime, time

    from custom_components.smart_rce.application.hourly_forecast import (
        HourlyForecastProvider,
    )
    from custom_components.smart_rce.garden.application.non_work_service import (
        NonWorkService,
    )
    from custom_components.smart_rce.garden.infrastructure.luba_state_reader import (
        LubaStateReader,
    )
    from custom_components.smart_rce.garden.infrastructure.non_work_reader import (
        NonWorkReader,
    )

_LOGGER = logging.getLogger(__name__)

_ONE_DAY: Final = timedelta(days=1)


class MowingPlannerService:
    """Computes and caches the planner decision; notifies entities on change."""

    def __init__(
        self,
        luba: LubaStateReader,
        forecast: HourlyForecastProvider,
        non_work: NonWorkService,
        non_work_fallback: NonWorkReader,
        now_provider: Callable[[], datetime],
    ) -> None:
        self._planner = MowingPlanner()
        self._luba = luba
        self._forecast = forecast
        self._non_work = non_work
        self._non_work_fallback = non_work_fallback
        self._now = now_provider
        self._decision: PlannerDecision | None = None
        self._listeners: list[Callable[[], None]] = []

    @property
    def decision(self) -> PlannerDecision | None:
        """Latest planner decision (None until the first recompute)."""
        return self._decision

    @callback
    def recompute(self) -> None:
        """Re-run the planner on current inputs; notify when the decision changed."""
        now = self._now()
        decision = self._planner.decide(
            MowingInput(
                battery=self._luba.read_battery(),
                progress=self._luba.read_progress(),
                at_dock=self._luba.read_at_dock(),
                now=now,
                slots=parse_forecast_slots(self._forecast.forecast_hourly),
                non_work_start=self._next_non_work_start(now),
            )
        )
        if decision == self._decision:
            return
        self._decision = decision
        for listener in list(self._listeners):
            listener()

    def _next_non_work_start(self, now: datetime) -> datetime | None:
        """Upcoming quiet-hours start: today at `start`, or tomorrow if past.

        Prefers the HA-owned target (garden 2a); falls back to the cloud sensor
        while the target is unset (fresh install) — with a warning, since the
        cloud value is the untrusted side.
        """
        start = self._non_work_start_time()
        if start is None:
            return None
        candidate = now.replace(
            hour=start.hour, minute=start.minute, second=0, microsecond=0
        )
        if candidate < now:
            candidate += _ONE_DAY
        return candidate

    def _non_work_start_time(self) -> time | None:
        target_start = self._non_work.start
        if target_start is not None:
            return target_start
        cloud = self._non_work_fallback.read_non_work_hours()
        if cloud is not None:
            _LOGGER.warning(
                "MowingPlannerService: non-work target unset — falling back to "
                "cloud-reported %s (set time.luba_non_work_start/end)",
                cloud.start,
            )
            return cloud.start
        return None

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to decision changes; returns unsubscribe."""
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove
