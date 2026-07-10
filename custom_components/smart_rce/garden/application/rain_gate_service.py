"""RainGateService — overrides device non-work to keep the mower off wet grass.

Coordinates the slices the gate spans: the user-target window from
`NonWorkService`, `dry_at` from `RainService`, and the mower state from
`LubaStateReader` (`docked_with_task` = at dock AND progress > 0, so a mid-day
block only preempts a charge-resume, never disturbs an active mow). Runs the
pure `RainGate` and, on an override change, pushes the window to the device via
the shared `NonWorkActuator` — restoring the plain target once dry. Notifies
listeners so `binary_sensor.luba_resume_into_wet` and the drift sensor recompute.

Wired (factory) to rain changes, non-work target changes, MOWER changes (a
mid-task dock asserts the block while charging) and a minute tick. The actuator
write happens only on an override transition (`RainGate` anti-churn), so the
300-sends/24h budget sees few writes per rain spell plus a restore.
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
    from custom_components.smart_rce.garden.infrastructure.luba_state_reader import (
        LubaStateReader,
    )
    from custom_components.smart_rce.garden.infrastructure.non_work_actuator import (
        NonWorkActuator,
    )
    from custom_components.smart_rce.infrastructure.async_task_runner import (
        AsyncTaskRunner,
    )


class RainGateService(Listenable):
    """Evaluates the rain gate and pushes the overridden non-work window on change."""

    def __init__(
        self,
        non_work: NonWorkService,
        rain: RainService,
        actuator: NonWorkActuator,
        luba: LubaStateReader,
        tasks: AsyncTaskRunner,
        now_provider: Callable[[], datetime],
    ) -> None:
        super().__init__()
        self._gate = RainGate()
        self._non_work = non_work
        self._rain = rain
        self._actuator = actuator
        self._luba = luba
        self._tasks = tasks
        self._now = now_provider

    @property
    def is_holding(self) -> bool:
        """True while non-work is extended past the user target (gate active)."""
        return self._gate.is_holding

    @callback
    def clear_hold(self) -> None:
        """User-initiated release (dashboard button) → restore target to device.

        For "the grass is actually fine, resume now". Pushes the target back
        and notifies. If it is still confirmed-wet near the morning boundary
        the next tick may re-hold; releasing mid-hold (outside the quiet
        window) sticks. To EXTEND instead, raise `number.garden_dry_out_hours`.
        """
        base = self._non_work.effective_hours
        if base is not None and self._gate.release():
            self._push(base)
            self._notify_all()

    @property
    def override(self) -> NonWorkHours | None:
        """The non-work window currently overridden onto the device, or None."""
        return self._gate.override

    @callback
    def evaluate(self) -> None:
        """Recompute the override; on change, push the window and notify."""
        base = self._non_work.effective_hours
        if base is None:
            return  # no target yet — nothing to override or restore
        docked_with_task = self._luba.read_at_dock() and self._luba.read_progress() > 0
        changed = self._gate.evaluate(
            self._now(), base, self._rain.dry_at, docked_with_task
        )
        if not changed:
            return
        self._push(base)
        self._notify_all()

    def _push(self, base: NonWorkHours) -> None:
        hours = self._gate.override or base
        self._tasks.run_background(
            self._actuator.apply(hours), name="garden_rain_gate_push"
        )
