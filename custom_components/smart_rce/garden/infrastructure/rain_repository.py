"""RainRepository — owns + persists RainState (rain-end timestamp + dry-out policy).

Driven adapter for HA Store (extends `Repository[RainState]`). Mirrors
`NonWorkRepository`. Persists across restarts so a recent rain end (and the
configured dry-out hours) survive reloads — the dry-out clock must not reset on
restart. Depends only on the shared technical base, not on `ems` (ADR-024).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from custom_components.smart_rce.garden.domain.rain import RainState
from custom_components.smart_rce.infrastructure.repository import Repository

if TYPE_CHECKING:
    from custom_components.smart_rce.infrastructure.async_task_runner import (
        AsyncTaskRunner,
    )
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class RainRepository(Repository[RainState]):
    """Persists RainState via HA Store. Owns the rain aggregate."""

    STORAGE_KEY = "garden_rain"

    def __init__(self, hass: HomeAssistant, tasks: AsyncTaskRunner) -> None:
        super().__init__(hass, tasks)
        self._state = RainState()

    @property
    def state(self) -> RainState:
        return self._state

    def _get_aggregate(self) -> RainState:
        return self._state

    async def async_restore(self) -> None:
        """Load persisted rain state (call once before first use)."""
        data: dict[str, Any] | None = await self._store.async_load()
        if data is not None:
            self._state = RainState.from_dict(data)
            self._last_saved = data
            _LOGGER.debug(
                "RainRepository: restored rain_ended_at=%s dry_hours=%s",
                self._state.rain_ended_at,
                self._state.dry_hours,
            )
