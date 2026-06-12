"""NonWorkService — application service for the garden non-work target.

Mediates UI (time entities) ↔ repository and tracks drift against the
mammotion cloud sensor. Observe-first design (2026-06-12): HA never writes
to the device — drift is only *reported* (binary sensor → Telegram alert).
The dormant `NonWorkActuator` returns in phase 2 (auto-reassert) once drift
data proves the cloud feed is trustworthy enough.

There is deliberately no seed-from-cloud: the cloud sensor serves ghost
values (multi-day-old redelivered snapshots), so adopting its value as our
target would consecrate garbage. The user sets the target once in the UI.

`set_start`/`set_end` compose a full `NonWorkHours` from the single value a
`time` entity supplies plus the other edge — from the current target when one
exists, otherwise from a pending in-memory edge (first-time setup: the target
is created when both edges have been entered; a restart mid-entry simply
forgets the single pending edge).
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


class NonWorkService(Service[NonWorkRepository]):
    """Owns non-work target mutations + drift detection against the cloud."""

    def __init__(self, repo: NonWorkRepository) -> None:
        super().__init__(repo)
        self._pending_start: time | None = None
        self._pending_end: time | None = None
        self._cloud: NonWorkHours | None = None

    @property
    def start(self) -> time | None:
        target = self._repo.schedule.target
        return target.start if target else self._pending_start

    @property
    def end(self) -> time | None:
        target = self._repo.schedule.target
        return target.end if target else self._pending_end

    async def set_start(self, start: time) -> None:
        target = self._repo.schedule.target
        if target is not None:
            await self.set_target(NonWorkHours(start, target.end))
            return
        self._pending_start = start
        await self._set_target_when_both_edges_pending()

    async def set_end(self, end: time) -> None:
        target = self._repo.schedule.target
        if target is not None:
            await self.set_target(NonWorkHours(target.start, end))
            return
        self._pending_end = end
        await self._set_target_when_both_edges_pending()

    async def _set_target_when_both_edges_pending(self) -> None:
        if self._pending_start is not None and self._pending_end is not None:
            await self.set_target(NonWorkHours(self._pending_start, self._pending_end))
        else:
            self._notify_all()  # reflect the lone pending edge in the UI

    async def set_target(self, hours: NonWorkHours) -> None:
        self._pending_start = None
        self._pending_end = None
        await self._persist_and_notify(self._repo.schedule.set_target(hours))

    @property
    def drift(self) -> bool:
        """True when the cloud reports a different window than our target.

        False without an opinion when either side is unknown (target not yet
        set by the user, or the cloud sensor unavailable/unparsable).
        """
        target = self._repo.schedule.target
        return target is not None and self._cloud is not None and self._cloud != target

    @property
    def cloud(self) -> NonWorkHours | None:
        """Last parsed value of the mammotion non-work sensor."""
        return self._cloud

    def update_cloud_state(self, current: NonWorkHours | None) -> None:
        """React to a mammotion sensor change (wired in factory).

        Stores the parsed cloud value and wakes entity listeners so the drift
        binary sensor recomputes. No persistence — cloud state is volatile
        observation, not part of the aggregate.
        """
        if current == self._cloud:
            return
        self._cloud = current
        self._notify_all()
