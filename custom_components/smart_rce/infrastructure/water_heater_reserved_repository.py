"""WaterHeaterReservedRepository — owns + persists WaterHeaterReservedPolicy.

Extends `Repository[WaterHeaterReservedPolicy]`. Persisted state: mode +
manual_value only — auto cache lives in WaterHeaterReservedService.

User-facing mutators are NOT on the repository — they are on the service
(Service[TRepo] base provides `_persist_and_notify` helper that mutates
the aggregate's policy and then calls `repo.persist()`). The repo just
owns the aggregate and the Store.

Two-phase init:
1. `__init__(hass, tasks)` — constructs default policy + Store
2. `await repo.async_restore()` — loads persisted state if present
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from ..domain.water_heater_reserved_policy import WaterHeaterReservedPolicy
from .async_task_runner import AsyncTaskRunner
from .repository import Repository

_LOGGER = logging.getLogger(__name__)


class WaterHeaterReservedRepository(Repository[WaterHeaterReservedPolicy]):
    """Persists WaterHeaterReservedPolicy via HA Store. Owns the policy."""

    STORAGE_KEY = "ems_water_heater_reserved"

    def __init__(self, hass: HomeAssistant, tasks: AsyncTaskRunner) -> None:
        super().__init__(hass, tasks)
        self._policy: WaterHeaterReservedPolicy = WaterHeaterReservedPolicy()

    @property
    def policy(self) -> WaterHeaterReservedPolicy:
        return self._policy

    def _get_aggregate(self) -> WaterHeaterReservedPolicy:
        return self._policy

    async def async_restore(self) -> None:
        """Call ONCE before Ems first tick. Replaces default policy with persisted."""
        data: dict[str, Any] | None = await self._store.async_load()
        if data is not None:
            self._policy = WaterHeaterReservedPolicy.from_dict(data)
            self._last_saved = data
            _LOGGER.debug(
                "WaterHeaterReservedRepository: restored from %s "
                "(mode=%s, manual_value=%s)",
                self.STORAGE_KEY,
                self._policy.mode.value,
                self._policy.manual_value,
            )
