"""MowingHoldService — overrides device non-work to keep the mower parked.

Coordinates the slices the hold spans: the user-target window from
`NonWorkService`, `dry_at` from `RainService`, and the mower state from
`LubaStateReader` (`docked_with_task` = at dock AND progress > 0, so the rain
hold only preempts a charge-resume, never disturbs an active mow). Runs the
`MowingHold` aggregate (owned by `MowingHoldRepository`, so the manual-park
deadline survives restarts) and, on an override change, pushes the window to the
device via the shared `NonWorkActuator` — restoring the plain target once no hold
is active. Notifies listeners so `binary_sensor.mowing_hold` and the drift sensor
recompute.

User actions (`park`, `cancel_park`, `clear_hold`) mutate + persist + re-evaluate
with `force=True` so the device window reflects them immediately (the tick-driven
`evaluate` keeps anti-churn). The actuator write happens only on an override
transition, so the 300-sends/24h budget sees few writes per rain spell / park.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.smart_rce.application.service import Service
from custom_components.smart_rce.garden.domain.non_work import NonWorkHours
from custom_components.smart_rce.garden.infrastructure.mowing_hold_repository import (
    MowingHoldRepository,
)
from homeassistant.core import callback

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from custom_components.smart_rce.garden.application.non_work_service import (
        NonWorkService,
    )
    from custom_components.smart_rce.garden.application.rain_service import RainService
    from custom_components.smart_rce.garden.domain.mowing_hold import MowingHold
    from custom_components.smart_rce.garden.infrastructure.luba_state_reader import (
        LubaStateReader,
    )
    from custom_components.smart_rce.garden.infrastructure.non_work_actuator import (
        NonWorkActuator,
    )
    from custom_components.smart_rce.infrastructure.async_task_runner import (
        AsyncTaskRunner,
    )


class MowingHoldService(Service[MowingHoldRepository]):
    """Evaluates the mowing hold and pushes the overridden non-work window."""

    def __init__(
        self,
        repo: MowingHoldRepository,
        non_work: NonWorkService,
        rain: RainService,
        actuator: NonWorkActuator,
        luba: LubaStateReader,
        tasks: AsyncTaskRunner,
        now_provider: Callable[[], datetime],
    ) -> None:
        super().__init__(repo)
        self._non_work = non_work
        self._rain = rain
        self._actuator = actuator
        self._luba = luba
        self._tasks = tasks
        self._now = now_provider

    @property
    def _hold(self) -> MowingHold:
        return self._repo.state

    @property
    def is_holding(self) -> bool:
        """True while the device non-work is overridden (any hold active)."""
        return self._hold.is_holding

    @property
    def is_manual_parked(self) -> bool:
        """True while a manual park is armed and not yet expired."""
        until = self._hold.manual_until
        return until is not None and self._now() < until

    @property
    def manual_until(self) -> datetime | None:
        """The manual-park deadline, or None."""
        return self._hold.manual_until

    @property
    def override(self) -> NonWorkHours | None:
        """The non-work window currently overridden onto the device, or None."""
        return self._hold.override

    @callback
    def park(self, minutes: int) -> None:
        """Arm a manual park for `minutes` (dashboard button) → hold + persist."""
        if self._hold.set_manual(self._now(), minutes):
            self._repo.save_if_changed()
        self._reevaluate(force=True)

    @callback
    def cancel_park(self) -> None:
        """Drop the manual park (dashboard button). Rain may still hold."""
        if self._hold.cancel_manual():
            self._repo.save_if_changed()
        self._reevaluate(force=True)

    @callback
    def clear_hold(self) -> None:
        """User-initiated rain release (dashboard button) → resume unless parked.

        For "the grass is actually fine, resume now". Suppresses the rain reason
        for the grace window so the mower can undock; a manual park is NOT
        affected (use `cancel_park` for that). To EXTEND the dry-out instead,
        raise `number.garden_dry_out_hours`.
        """
        self._hold.suppress_rain(self._now())
        self._reevaluate(force=True)

    @callback
    def evaluate(self) -> None:
        """Tick/listener entry — recompute with anti-churn (no force)."""
        self._reevaluate(force=False)

    def _reevaluate(self, *, force: bool) -> None:
        base = self._non_work.effective_hours
        if base is None:
            return  # no target yet — nothing to override or restore
        docked_with_task = self._luba.read_at_dock() and self._luba.read_progress() > 0
        changed = self._hold.evaluate(
            self._now(), base, self._rain.dry_at, docked_with_task, force=force
        )
        if not changed:
            return
        self._push(base)
        self._notify_all()

    def _push(self, base: NonWorkHours) -> None:
        hours = self._hold.override or base
        self._tasks.run_background(
            self._actuator.apply(hours), name="garden_mowing_hold_push"
        )
