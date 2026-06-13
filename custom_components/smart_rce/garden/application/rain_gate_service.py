"""RainGateService — auto-extends non-work past the boundary while grass is wet.

Coordinates the two slices the gate spans: it pulls the user-target window from
`NonWorkService` and the wet/dry-out state from `RainService`, runs the pure
`RainGate` near the morning boundary, and on a hold change pushes the result to
the device via the shared `NonWorkActuator` — the rain-extended end while wet,
or the plain target to restore once dry. Notifies listeners so
`binary_sensor.luba_resume_into_wet` and the drift sensor recompute.

Wired (factory) to the same triggers as the planner — rain changes, non-work
target changes, and a minute tick (the boundary is a function of `now`).
Evaluation is cheap; the actuator write happens only on a hold transition, so
the 300-sends/24h budget sees ~one write per dry-out period plus a restore.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.smart_rce.application.listenable import Listenable
from custom_components.smart_rce.garden.domain.non_work import NonWorkHours
from custom_components.smart_rce.garden.domain.rain_gate import RainGate
from homeassistant.core import callback

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from custom_components.smart_rce.garden.application.non_work_service import (
        NonWorkService,
    )
    from custom_components.smart_rce.garden.application.rain_service import RainService
    from custom_components.smart_rce.garden.infrastructure.non_work_actuator import (
        NonWorkActuator,
    )
    from custom_components.smart_rce.infrastructure.async_task_runner import (
        AsyncTaskRunner,
    )


class RainGateService(Listenable):
    """Evaluates the rain gate and pushes the effective non-work end on change."""

    def __init__(
        self,
        non_work: NonWorkService,
        rain: RainService,
        actuator: NonWorkActuator,
        tasks: AsyncTaskRunner,
        now_provider: Callable[[], datetime],
    ) -> None:
        super().__init__()
        self._gate = RainGate()
        self._non_work = non_work
        self._rain = rain
        self._actuator = actuator
        self._tasks = tasks
        self._now = now_provider

    @property
    def is_holding(self) -> bool:
        """True while non-work is extended past the user target (gate active)."""
        return self._gate.is_holding

    @property
    def hold_until(self) -> datetime | None:
        """The extended non-work end currently asserted, or None when restored."""
        return self._gate.hold_until

    @callback
    def evaluate(self) -> None:
        """Recompute the hold; on change, push the effective end and notify."""
        base = self._non_work.effective_hours
        if base is None:
            return  # no target yet — nothing to extend or restore
        now = self._now()
        changed = self._gate.evaluate(
            now,
            base.end_of_active_window(now),
            self._rain.currently_wet,
            self._rain.dry_at,
            self._rain.dry_hours,
        )
        if not changed:
            return
        self._push(base)
        self._notify_all()

    def _push(self, base: NonWorkHours) -> None:
        hold = self._gate.hold_until
        hours = base if hold is None else NonWorkHours(base.start, hold.time())
        self._tasks.run_background(
            self._actuator.apply(hours), name="garden_rain_gate_push"
        )
