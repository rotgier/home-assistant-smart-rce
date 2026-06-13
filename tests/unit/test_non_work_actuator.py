"""Unit tests for NonWorkActuator — the only write path to the device.

Covers the state-diff guard (skip when no target / already in sync, push on
drift) and the `mammotion.set_non_work_hours` payload shape.
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


def _actuator(
    target: NonWorkHours | None,
    sensor: NonWorkHours | None,
) -> tuple[NonWorkActuator, MagicMock]:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    repo = MagicMock()
    repo.schedule = NonWorkSchedule(target)
    reader = MagicMock()
    reader.read_non_work_hours.return_value = sensor
    return NonWorkActuator(hass, repo, reader), hass


async def test_no_target_skips_write() -> None:
    actuator, hass = _actuator(target=None, sensor=TARGET)

    await actuator.apply()

    hass.services.async_call.assert_not_called()


async def test_device_already_in_sync_skips_write() -> None:
    actuator, hass = _actuator(target=TARGET, sensor=TARGET)

    await actuator.apply()

    hass.services.async_call.assert_not_called()


async def test_drift_pushes_target_with_payload() -> None:
    actuator, hass = _actuator(
        target=TARGET, sensor=NonWorkHours(time(21, 0), time(9, 0))
    )

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


async def test_unavailable_sensor_counts_as_drift_and_pushes() -> None:
    actuator, hass = _actuator(target=TARGET, sensor=None)

    await actuator.apply()

    hass.services.async_call.assert_awaited_once()
