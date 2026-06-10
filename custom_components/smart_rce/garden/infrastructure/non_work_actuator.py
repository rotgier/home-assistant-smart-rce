"""NonWorkActuator — driven adapter syncing the garden non-work target to mammotion.

HA is the source of truth. The garden-owned target (`NonWorkRepository`) is
pushed to the robot via the `mammotion.set_non_work_hours` service. Reconcile is
state-diff: write only when the target differs from what the sensor currently
reports. The mammotion integration optimistically updates that sensor right
after the write, so a no-op diff closes the loop — no own cache needed
(ADR-024; contrast `BatteryChargeCurrentActuator`, which caches because goodwe
exposes no readback entity).

Startup seed: on HA start, if the target is still unset (fresh install / empty
storage), seed it from the current sensor value so the first deploy does NOT
overwrite the robot's existing setting (target := cloud → diff=0 → no write).
Sensor unavailable at startup (mammotion not loaded) → skip; a later restart or
a manual set populates it.

No periodic drift tick — non-work changes rarely and the cloud read is
rate-limited. Reconcile fires on startup seed + on each target change
(orchestrated by `NonWorkService`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from custom_components.smart_rce.garden.infrastructure.non_work_reader import (
    read_non_work_hours,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState

if TYPE_CHECKING:
    from custom_components.smart_rce.garden.domain.non_work import NonWorkHours
    from custom_components.smart_rce.garden.infrastructure.non_work_repository import (
        NonWorkRepository,
    )
    from custom_components.smart_rce.infrastructure.async_task_runner import (
        AsyncTaskRunner,
    )
    from homeassistant.core import Event, HomeAssistant

_LOGGER = logging.getLogger(__name__)

MAMMOTION_DOMAIN = "mammotion"
SERVICE_SET_NON_WORK_HOURS = "set_non_work_hours"


class NonWorkActuator:
    """Pushes the garden non-work target to mammotion (state-diff reconcile)."""

    def __init__(
        self,
        hass: HomeAssistant,
        repo: NonWorkRepository,
        tasks: AsyncTaskRunner,
        *,
        sensor_id: str,
        mower_id: str,
    ) -> None:
        self._hass = hass
        self._repo = repo
        self._sensor_id = sensor_id
        self._mower_id = mower_id
        self._lock = asyncio.Lock()

        if hass.state == CoreState.running:
            tasks.run_background(
                self._seed_from_cloud(), name="smart_rce_garden_non_work_seed"
            )
        else:
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._on_ha_started)

    async def _on_ha_started(self, _event: Event) -> None:
        await self._seed_from_cloud()

    async def _seed_from_cloud(self) -> None:
        """Seed target from the current sensor when unset — never overwrites."""
        async with self._lock:
            if self._repo.schedule.target is not None:
                return
            current = read_non_work_hours(self._hass, self._sensor_id)
            if current is None:
                _LOGGER.debug(
                    "NonWorkActuator: sensor unavailable at startup — seed skipped"
                )
                return
            if self._repo.schedule.set_target(current):
                await self._repo.persist()
                _LOGGER.debug("NonWorkActuator: seeded target from cloud=%s", current)

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
