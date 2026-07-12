"""MowingPlannerService — orchestrates the mowing planner (garden 2b).

Pulls planner inputs from injected ports on every `recompute()` (wired in the
factory to: Luba telemetry changes, forecast updates, non-work target changes
and a 1-minute tick — `should_start` compares `now` against `opt_start`, so
time itself is an input) and exposes the latest `PlannerDecision` to the
sensor entities. Notifies listeners only when the decision actually changed,
so the minutely tick is free while nothing moves.

No hass and no entity ids here: telemetry comes from `LubaStateReader`,
forecast from `ForecastReader` (which owns the ems-published cross-context
port), quiet hours from `NonWorkService.effective_hours`, and the grass
dry-out floor from `RainService.dry_at` (= last rain end + dry_hours). The
planner clamps its window start to the latest of now / active-quiet-end /
dry_at; calendar + dry-out math live in the domain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.smart_rce.application.service import Service
from custom_components.smart_rce.garden.domain.mowing_planner import (
    MowingInput,
    MowingPlanner,
    PlannerDecision,
)
from custom_components.smart_rce.garden.infrastructure.mowing_policy_repository import (
    MowingPolicyRepository,
)
from homeassistant.core import callback

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from custom_components.smart_rce.garden.application.non_work_service import (
        NonWorkService,
    )
    from custom_components.smart_rce.garden.application.rain_service import RainService
    from custom_components.smart_rce.garden.infrastructure.forecast_reader import (
        ForecastReader,
    )
    from custom_components.smart_rce.garden.infrastructure.luba_state_reader import (
        LubaStateReader,
    )


class MowingPlannerService(Service[MowingPolicyRepository]):
    """Computes and caches the planner decision; notifies entities on change."""

    def __init__(
        self,
        repo: MowingPolicyRepository,
        luba: LubaStateReader,
        forecast: ForecastReader,
        non_work: NonWorkService,
        rain: RainService,
        now_provider: Callable[[], datetime],
    ) -> None:
        super().__init__(repo)
        self._planner = MowingPlanner()
        self._luba = luba
        self._forecast = forecast
        self._non_work = non_work
        self._rain = rain
        self._now = now_provider
        self._decision: PlannerDecision | None = None

    @property
    def decision(self) -> PlannerDecision | None:
        """Latest planner decision (None until the first recompute)."""
        return self._decision

    @property
    def fresh_start_battery(self) -> int:
        """SoC threshold above which a fresh program starts (tunable via number)."""
        return self._repo.state.fresh_start_battery

    def set_fresh_start_battery(self, value: int) -> None:
        if self._repo.state.set_fresh_start_battery(value):
            self._repo.save_if_changed()
        self.recompute()

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
                dry_at=self._rain.dry_at,
                time_left_min=self._luba.read_time_left(),
                fresh_start_battery=self._repo.state.fresh_start_battery,
            )
        )
        if decision == self._decision:
            return
        self._decision = decision
        self._notify_all()
