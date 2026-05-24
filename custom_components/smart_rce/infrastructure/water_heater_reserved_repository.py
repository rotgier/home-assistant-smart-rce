"""WaterHeaterReservedRepository — owns + persists WaterHeaterReservedPolicy.

Extends `Repository[WaterHeaterReservedPolicy]` (parity with
BatteryChargeRepository). Persisted state: mode + manual_value only —
auto cache lives in WaterHeaterReservedService.

Async mutators (`set_mode`, `set_manual_value`) auto-persist *immediately*
via `await self.persist()` so NumberEntity / SelectEntity UI changes
become durable before HA returns the service-call response (~1s crash
safety per ADR-018).

Two-phase init:
1. `__init__(hass, tasks)` — constructs default policy + Store
2. `await repo.async_restore()` — loads persisted state if present
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from ..domain.water_heater_reserved_policy import (
    ReservedMode,
    WaterHeaterReservedPolicy,
)
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

    async def set_mode(self, mode: ReservedMode) -> None:
        """Change mode + auto-persist on change."""
        if self._policy.set_mode(mode):
            await self.persist()

    async def set_manual_value(self, value: int) -> None:
        """Change manual value + auto-persist on change."""
        if self._policy.set_manual_value(value):
            await self.persist()
