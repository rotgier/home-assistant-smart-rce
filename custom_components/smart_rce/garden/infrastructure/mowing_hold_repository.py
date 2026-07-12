"""MowingHoldRepository — owns + persists MowingHold (the manual-park deadline).

Driven adapter for HA Store (extends `Repository[MowingHold]`). Mirrors
`RainRepository`. Only `manual_until` persists: a manual park must survive an HA
restart (else the mower resumes into whatever the user parked it away from — the
kids' football game). The rain override and suppression window are transient
(re-derived on the next `evaluate`). Depends only on the shared technical base,
not on `ems` (ADR-024).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from custom_components.smart_rce.garden.domain.mowing_hold import MowingHold
from custom_components.smart_rce.infrastructure.repository import Repository

if TYPE_CHECKING:
    from custom_components.smart_rce.infrastructure.async_task_runner import (
        AsyncTaskRunner,
    )
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class MowingHoldRepository(Repository[MowingHold]):
    """Persists MowingHold via HA Store. Owns the mowing-hold aggregate."""

    STORAGE_KEY = "garden_mowing_hold"

    def __init__(self, hass: HomeAssistant, tasks: AsyncTaskRunner) -> None:
        super().__init__(hass, tasks)
        self._state = MowingHold()

    @property
    def state(self) -> MowingHold:
        return self._state

    def _get_aggregate(self) -> MowingHold:
        return self._state

    async def async_restore(self) -> None:
        """Load persisted manual-park deadline (call once before first use)."""
        data: dict[str, Any] | None = await self._store.async_load()
        if data is not None:
            self._state = MowingHold.from_dict(data)
            self._last_saved = data
            _LOGGER.debug(
                "MowingHoldRepository: restored manual_until=%s",
                self._state.manual_until,
            )
