"""NonWorkService — application service for the garden non-work target.

Mediates UI (time entities) ↔ repository and explicitly orchestrates the
actuator (smart_rce convention: services drive their adapters). Entities
subscribe via `add_listener` to refresh on any target change — both UI edits
and the startup seed.

`set_start`/`set_end` compose a full `NonWorkHours` from the single value a
`time` entity supplies plus the other edge from the current target. They no-op
until a target exists (before the startup seed) — entities are unavailable then.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.smart_rce.application.service import Service
from custom_components.smart_rce.garden.domain.non_work import NonWorkHours
from custom_components.smart_rce.garden.infrastructure.non_work_repository import (
    NonWorkRepository,
)

if TYPE_CHECKING:
    from datetime import time

    from custom_components.smart_rce.garden.infrastructure.non_work_actuator import (
        NonWorkActuator,
    )


class NonWorkService(Service[NonWorkRepository]):
    """Owns non-work target mutations: persist + notify entities + drive actuator."""

    def __init__(self, repo: NonWorkRepository, actuator: NonWorkActuator) -> None:
        super().__init__(repo)
        self._actuator = actuator

    async def set_start(self, start: time) -> None:
        target = self._repo.schedule.target
        if target is not None:
            await self.set_target(NonWorkHours(start, target.end))

    async def set_end(self, end: time) -> None:
        target = self._repo.schedule.target
        if target is not None:
            await self.set_target(NonWorkHours(target.start, end))

    async def set_target(self, hours: NonWorkHours) -> None:
        changed = self._repo.schedule.set_target(hours)
        await self._persist_and_notify(changed)
        if changed:
            await self._actuator.apply()

    async def reconcile_from_cloud(self, current: NonWorkHours | None) -> None:
        """React to a mammotion sensor change (wired in factory).

        Seeds the target when unset (fresh install — adopt the robot's current
        value, so we never overwrite it), or reasserts our target when the cloud
        drifted from it (HA is the source of truth). Listener-driven, so it
        works whenever the sensor becomes available — no startup-timing race.
        """
        if current is None:
            return
        target = self._repo.schedule.target
        if target is None:
            await self.set_target(current)  # seed → apply diff=0, no write
        elif current != target:
            await self._actuator.apply()  # drift → reassert our target
