"""NonWorkRepository — owns + persists NonWorkSchedule (garden non-work target).

Driven adapter for HA Store (extends `Repository[NonWorkSchedule]`). Service +
Actuator depend on this repo to read the target + persist it — siblings, no
circular dependency. HA is the source of truth: the user sets the target via
the time entities; the cloud sensor is only observed (drift), never seeded
into the schedule (observe-first — the cloud feed is untrusted).

Depends only on the shared technical base (`Repository`, `AsyncTaskRunner`) —
not on the `ems` bounded context (per ADR-024).

Two-phase init:
1. `__init__(hass, tasks)` — constructs empty schedule (target=None) + Store
2. `await repo.async_restore()` — loads persisted target if present
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from custom_components.smart_rce.garden.domain.non_work import NonWorkSchedule
from custom_components.smart_rce.infrastructure.repository import Repository

if TYPE_CHECKING:
    from custom_components.smart_rce.infrastructure.async_task_runner import (
        AsyncTaskRunner,
    )
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class NonWorkRepository(Repository[NonWorkSchedule]):
    """Persists NonWorkSchedule via HA Store. Owns the schedule aggregate."""

    STORAGE_KEY = "garden_non_work"

    def __init__(self, hass: HomeAssistant, tasks: AsyncTaskRunner) -> None:
        super().__init__(hass, tasks)
        self._schedule = NonWorkSchedule()

    @property
    def schedule(self) -> NonWorkSchedule:
        return self._schedule

    def _get_aggregate(self) -> NonWorkSchedule:
        return self._schedule

    async def async_restore(self) -> None:
        """Load persisted schedule (call once before first use)."""
        data: dict[str, Any] | None = await self._store.async_load()
        if data is not None:
            self._schedule = NonWorkSchedule.from_dict(data)
            self._last_saved = data
            _LOGGER.debug(
                "NonWorkRepository: restored target=%s", self._schedule.target
            )
