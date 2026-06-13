"""MowingPlannerService — orchestrates the mowing planner (garden 2b).

Pulls planner inputs from injected ports on every `recompute()` (wired in the
factory to: Luba telemetry changes, forecast updates, non-work target changes
and a 1-minute tick — `should_start` compares `now` against `opt_start`, so
time itself is an input) and exposes the latest `PlannerDecision` to the
sensor entities. Notifies listeners only when the decision actually changed,
so the minutely tick is free while nothing moves.

No hass and no entity ids here: telemetry comes from `LubaStateReader`,
forecast from `ForecastReader` (which owns the ems-published cross-context
port), quiet hours from `NonWorkService.effective_hours` (target-or-cloud
source selection is NonWorkService's concern; calendar math lives on the
`NonWorkHours` domain VO and is derived inside the planner).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.smart_rce.application.listenable import Listenable
from custom_components.smart_rce.garden.domain.mowing_planner import (
    MowingInput,
    MowingPlanner,
    PlannerDecision,
)
from homeassistant.core import callback

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from custom_components.smart_rce.garden.application.non_work_service import (
        NonWorkService,
    )
    from custom_components.smart_rce.garden.infrastructure.forecast_reader import (
        ForecastReader,
    )
    from custom_components.smart_rce.garden.infrastructure.luba_state_reader import (
        LubaStateReader,
    )


class MowingPlannerService(Listenable):
    """Computes and caches the planner decision; notifies entities on change."""

    def __init__(
        self,
        luba: LubaStateReader,
        forecast: ForecastReader,
        non_work: NonWorkService,
        now_provider: Callable[[], datetime],
    ) -> None:
        super().__init__()
        self._planner = MowingPlanner()
        self._luba = luba
        self._forecast = forecast
        self._non_work = non_work
        self._now = now_provider
        self._decision: PlannerDecision | None = None

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
                slots=self._forecast.read_forecast_slots(),
                non_work=self._non_work.effective_hours,
            )
        )
        if decision == self._decision:
            return
        self._decision = decision
        self._notify_all()
