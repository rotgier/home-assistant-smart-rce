"""Unit tests for NonWorkActuator — the single write path to the device.

Always writes the hours it is given (no state-diff guard — that guard trusted
the laggy/ghosting cloud sensor and dropped legitimate re-asserts). Covers the
`set_non_work_hours` payload shape.
"""

from datetime import time
from unittest.mock import AsyncMock, MagicMock

from custom_components.smart_rce.garden.const import LUBA_LAWN_MOWER
from custom_components.smart_rce.garden.domain.non_work import NonWorkHours
from custom_components.smart_rce.garden.infrastructure.non_work_actuator import (
    MAMMOTION_DOMAIN,
    SERVICE_SET_NON_WORK_HOURS,
    NonWorkActuator,
)

TARGET = NonWorkHours(time(20, 35), time(10, 5))


def _actuator() -> tuple[NonWorkActuator, MagicMock]:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    return NonWorkActuator(hass), hass


async def test_writes_given_hours_with_payload() -> None:
    actuator, hass = _actuator()

    await actuator.apply(TARGET)

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


async def test_writes_every_call_no_diff_guard() -> None:
    actuator, hass = _actuator()

    await actuator.apply(TARGET)
    await actuator.apply(NonWorkHours(time(20, 35), time(13, 0)))

    assert hass.services.async_call.await_count == 2
