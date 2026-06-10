"""NonWorkActuator — driven adapter pushing the garden non-work target to mammotion.

HA is the source of truth. The garden-owned target (`NonWorkRepository`) is
pushed to the robot via `mammotion.set_non_work_hours`. `apply()` is state-diff:
write only when the target differs from what the sensor currently reports. The
mammotion integration optimistically updates that sensor after the write, so a
no-op diff closes the loop — no own cache needed (ADR-024; contrast
`BatteryChargeCurrentActuator`, which caches because goodwe exposes no readback).

Seed + drift handling live in `NonWorkService.reconcile_from_cloud`, driven by a
sensor state-change listener (wired in the factory). That avoids the startup
race of a one-shot `EVENT_HOMEASSISTANT_STARTED` seed: mammotion loads slowly,
so at HA-start the sensor is often still `None`; a listener reconciles whenever
the sensor actually becomes available.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from custom_components.smart_rce.garden.infrastructure.non_work_reader import (
    read_non_work_hours,
)

if TYPE_CHECKING:
    from custom_components.smart_rce.garden.domain.non_work import NonWorkHours
    from custom_components.smart_rce.garden.infrastructure.non_work_repository import (
        NonWorkRepository,
    )
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

MAMMOTION_DOMAIN = "mammotion"
SERVICE_SET_NON_WORK_HOURS = "set_non_work_hours"


class NonWorkActuator:
    """Pushes the garden non-work target to mammotion (state-diff reconcile)."""

    def __init__(
        self,
        hass: HomeAssistant,
        repo: NonWorkRepository,
        *,
        sensor_id: str,
        mower_id: str,
    ) -> None:
        self._hass = hass
        self._repo = repo
        self._sensor_id = sensor_id
        self._mower_id = mower_id
        self._lock = asyncio.Lock()

    async def apply(self) -> None:
        """Reconcile: push target to mammotion when it differs from the sensor."""
        async with self._lock:
            target = self._repo.schedule.target
            if target is None:
                return
            if read_non_work_hours(self._hass, self._sensor_id) == target:
                return
            await self._push(target)

    async def _push(self, target: NonWorkHours) -> None:
        _LOGGER.info("NonWorkActuator: set non-work %s-%s", target.start, target.end)
        await self._hass.services.async_call(
            MAMMOTION_DOMAIN,
            SERVICE_SET_NON_WORK_HOURS,
            {
                "entity_id": self._mower_id,
                "start_time": target.start.isoformat(timespec="minutes"),
                "end_time": target.end.isoformat(timespec="minutes"),
            },
            blocking=True,
        )
