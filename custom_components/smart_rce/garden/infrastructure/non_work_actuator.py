"""NonWorkActuator — driven adapter pushing the garden non-work target to mammotion.

DORMANT (2026-06-12): not wired in the factory. Phase 1 is observe-first —
drift between the HA target and the cloud sensor only raises
`binary_sensor.luba_non_work_drift` (alert automation notifies; no device
writes). The cloud feed proved untrustworthy (ghost redelivered snapshots)
and every `set_non_work_hours` consumes the 300-sends/24h MQTT budget, so
auto-reassert returns in phase 2 only if drift data shows it is needed —
then with a stable-mismatch debounce and a write cooldown.

HA is the source of truth. The garden-owned target (`NonWorkRepository`) is
pushed to the robot via `mammotion.set_non_work_hours`. `apply()` is state-diff:
write only when the target differs from what the sensor currently reports. The
mammotion integration optimistically updates that sensor after the write, so a
no-op diff closes the loop — no own cache needed (ADR-024; contrast
`BatteryChargeCurrentActuator`, which caches because goodwe exposes no readback).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Final

from custom_components.smart_rce.garden.const import LUBA_LAWN_MOWER

if TYPE_CHECKING:
    from custom_components.smart_rce.garden.domain.non_work import NonWorkHours
    from custom_components.smart_rce.garden.infrastructure.non_work_reader import (
        NonWorkReader,
    )
    from custom_components.smart_rce.garden.infrastructure.non_work_repository import (
        NonWorkRepository,
    )
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

MAMMOTION_DOMAIN = "mammotion"
SERVICE_SET_NON_WORK_HOURS = "set_non_work_hours"


class NonWorkActuator:
    """Pushes the garden non-work target to mammotion (state-diff reconcile)."""

    _MOWER_ID: Final[str] = LUBA_LAWN_MOWER

    def __init__(
        self,
        hass: HomeAssistant,
        repo: NonWorkRepository,
        reader: NonWorkReader,
    ) -> None:
        self._hass = hass
        self._repo = repo
        self._reader = reader
        self._lock = asyncio.Lock()

    async def apply(self) -> None:
        """Reconcile: push target to mammotion when it differs from the sensor."""
        async with self._lock:
            target = self._repo.schedule.target
            if target is None:
                return
            if self._reader.read_non_work_hours() == target:
                return
            await self._push(target)

    async def _push(self, target: NonWorkHours) -> None:
        _LOGGER.info("NonWorkActuator: set non-work %s-%s", target.start, target.end)
        await self._hass.services.async_call(
            MAMMOTION_DOMAIN,
            SERVICE_SET_NON_WORK_HOURS,
            {
                "entity_id": NonWorkActuator._MOWER_ID,
                "start_time": target.start.isoformat(timespec="minutes"),
                "end_time": target.end.isoformat(timespec="minutes"),
            },
            blocking=True,
        )
