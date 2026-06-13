"""Unit tests for NonWorkActuator — the only write path to the device.

Phase 1.5: the button always writes (no state-diff guard — that guard trusted
the laggy/ghosting cloud sensor and dropped legitimate re-asserts). No-op only
when there is no target. Also covers the `set_non_work_hours` payload shape.
"""

from datetime import time
from unittest.mock import AsyncMock, MagicMock

from custom_components.smart_rce.garden.const import LUBA_LAWN_MOWER
from custom_components.smart_rce.garden.domain.non_work import (
    NonWorkHours,
    NonWorkSchedule,
)
from custom_components.smart_rce.garden.infrastructure.non_work_actuator import (
    MAMMOTION_DOMAIN,
    SERVICE_SET_NON_WORK_HOURS,
    NonWorkActuator,
)

TARGET = NonWorkHours(time(20, 35), time(10, 5))


def _actuator(target: NonWorkHours | None) -> tuple[NonWorkActuator, MagicMock]:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    repo = MagicMock()
    repo.schedule = NonWorkSchedule(target)
    return NonWorkActuator(hass, repo), hass


async def test_no_target_skips_write() -> None:
    actuator, hass = _actuator(target=None)

    await actuator.apply()

    hass.services.async_call.assert_not_called()


async def test_always_writes_target_with_payload() -> None:
    actuator, hass = _actuator(target=TARGET)

    await actuator.apply()

    hass.services.async_call.assert_awaited_once_with(
        MAMMOTION_DOMAIN,
        SERVICE_SET_NON_WORK_HOURS,
        {
            "entity_id": LUBA_LAWN_MOWER,
            "start_time": "20:35",
            "end_time": "10:05",
        },
        blocking=True,
    )


async def test_writes_every_press_no_diff_guard() -> None:
    actuator, hass = _actuator(target=TARGET)

    await actuator.apply()
    await actuator.apply()

    assert hass.services.async_call.await_count == 2
