"""NonWorkActuator — driven adapter pushing the garden non-work target to mammotion.

Phase 1.5 (2026-06-12): fired ONLY by the user's dashboard push button
(`NonWorkService.push_to_device`) — no automatic writes. Drift between the
HA target and the cloud sensor only raises `binary_sensor.luba_non_work_drift`
(alert automation notifies). Auto-reassert returns in phase 2 only if drift
data shows it is needed — then with a stable-mismatch debounce and a write
cooldown.

`apply()` ALWAYS writes (when a target exists). It used to skip when the
cloud sensor already matched the target, but that sensor lags and ghosts
(redelivered multi-day-old snapshots), so the state-diff guard silently
dropped legitimate re-asserts — observed 2026-06-13: re-asserting 10:05 was
a no-op because the sensor still read a stale 10:05 after we'd just pushed
08:05. The button is an explicit user action; we honor it unconditionally.
Each press costs one of the 300-sends/24h MQTT budget. **Phase-2 auto-reassert
must dedup against an OWN last-pushed cache, NEVER the cloud sensor.**

HA is the source of truth: the garden-owned target (`NonWorkRepository`) is
pushed to the robot via `mammotion.set_non_work_hours`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Final

from custom_components.smart_rce.garden.const import LUBA_LAWN_MOWER

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
    """Pushes the garden non-work target to mammotion (user-initiated write)."""

    _MOWER_ID: Final[str] = LUBA_LAWN_MOWER

    def __init__(self, hass: HomeAssistant, repo: NonWorkRepository) -> None:
        self._hass = hass
        self._repo = repo
        self._lock = asyncio.Lock()

    async def apply(self) -> None:
        """Write the current target to mammotion. No-op only when unset."""
        async with self._lock:
            target = self._repo.schedule.target
            if target is None:
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
