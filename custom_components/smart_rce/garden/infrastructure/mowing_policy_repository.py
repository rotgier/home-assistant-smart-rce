"""MowingPolicyRepository — owns + persists MowingPolicy (planner thresholds).

Driven adapter for HA Store (extends `Repository[MowingPolicy]`). Mirrors
`RainRepository`. Persists `fresh_start_battery` so a tuned value survives
restarts — replacing the old RestoreNumber path, consistently with the other
domain-policy numbers (Store-backed). Depends only on the shared technical base,
not on `ems` (ADR-024).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from custom_components.smart_rce.garden.domain.mowing_policy import MowingPolicy
from custom_components.smart_rce.infrastructure.repository import Repository

if TYPE_CHECKING:
    from custom_components.smart_rce.infrastructure.async_task_runner import (
        AsyncTaskRunner,
    )
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class MowingPolicyRepository(Repository[MowingPolicy]):
    """Persists MowingPolicy via HA Store. Owns the mowing-policy aggregate."""

    STORAGE_KEY = "garden_mowing_policy"

    def __init__(self, hass: HomeAssistant, tasks: AsyncTaskRunner) -> None:
        super().__init__(hass, tasks)
        self._state = MowingPolicy()

    @property
    def state(self) -> MowingPolicy:
        return self._state

    def _get_aggregate(self) -> MowingPolicy:
        return self._state

    async def async_restore(self) -> None:
        """Load persisted planner policy (call once before first use)."""
        data: dict[str, Any] | None = await self._store.async_load()
        if data is not None:
            self._state = MowingPolicy.from_dict(data)
            self._last_saved = data
            _LOGGER.debug(
                "MowingPolicyRepository: restored fresh_start_battery=%s",
                self._state.fresh_start_battery,
            )
