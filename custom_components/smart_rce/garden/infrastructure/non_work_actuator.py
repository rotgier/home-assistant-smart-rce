"""NonWorkActuator — driven adapter pushing garden non-work hours to mammotion.

Single write path to the device, shared by two callers:
- the user's dashboard push button (`NonWorkService.push_to_device`) — pushes
  the HA target;
- the mowing hold (`MowingHoldService`) — pushes the rain-extended end near the
  morning boundary, then the target again to restore (garden 2d).

`apply(hours)` ALWAYS writes the hours it is given. It used to read the target
itself and skip when the cloud sensor matched, but that sensor lags and ghosts
(redelivered multi-day-old snapshots), so the state-diff guard silently dropped
legitimate re-asserts — observed 2026-06-13: re-asserting 10:05 was a no-op
because the sensor still read a stale 10:05 after we'd just pushed 08:05. The
caller decides WHAT to push; the actuator just writes it (serialized by a lock).
Each call costs one of the 300-sends/24h MQTT budget — callers must push only
on change. **Any auto-reassert dedup must compare against an OWN cache, NEVER
the cloud sensor.**

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
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

MAMMOTION_DOMAIN = "mammotion"
SERVICE_SET_NON_WORK_HOURS = "set_non_work_hours"


class NonWorkActuator:
    """Pushes given non-work hours to mammotion (serialized; always writes)."""

    _MOWER_ID: Final[str] = LUBA_LAWN_MOWER

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._lock = asyncio.Lock()

    async def apply(self, hours: NonWorkHours) -> None:
        """Write the given non-work hours to mammotion (one MQTT send)."""
        async with self._lock:
            _LOGGER.info("NonWorkActuator: set non-work %s-%s", hours.start, hours.end)
            await self._hass.services.async_call(
                MAMMOTION_DOMAIN,
                SERVICE_SET_NON_WORK_HOURS,
                {
                    "entity_id": NonWorkActuator._MOWER_ID,
                    "start_time": hours.start.isoformat(timespec="minutes"),
                    "end_time": hours.end.isoformat(timespec="minutes"),
                },
                blocking=True,
            )
