"""Garden binary sensors — non-work drift + mowing should-start + resume gate.

`binary_sensor.luba_non_work_drift` turns on when the mammotion non-work
sensor reports a different window than the user-set target (observe-first:
the alert automation notifies; nothing is written to the device). Off when
either side is unknown, and muted while the rain gate is holding — the gate
deliberately extends the device window past the target, so that mismatch is
expected, not drift (2d).

`binary_sensor.mowing_should_start` mirrors `PlannerDecision.should_start`
(garden 2b) — the trigger entity for the "Puść Lubę" alert automation.

`binary_sensor.luba_resume_into_wet` is on while the rain gate has extended
the non-work end past the user target (grass not yet dry near the boundary) —
the trigger entity for the "non-work przesunięte" alert automation (2d).

Top-level `binary_sensor.py` aggregates these via `build_binary_sensors`,
so garden owns its presentation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.smart_rce.const import DOMAIN
from custom_components.smart_rce.garden.garden_device import luba_device_info
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)

if TYPE_CHECKING:
    from custom_components.smart_rce import SmartRceConfigEntry


def build_binary_sensors(entry: SmartRceConfigEntry) -> list[BinarySensorEntity]:
    """Garden binary sensors for top-level `binary_sensor.py` to add."""
    return [
        LubaNonWorkDriftBinarySensor(entry),
        MowingShouldStartBinarySensor(entry),
        GardenGrassWetBinarySensor(entry),
        LubaResumeIntoWetBinarySensor(entry),
    ]


class LubaNonWorkDriftBinarySensor(BinarySensorEntity):
    """On when the device's non-work window differs from the HA target.

    Muted while the rain gate is holding: the gate intentionally pushes an
    extended end to the device, so the device-vs-target mismatch is expected.
    """

    _attr_has_entity_name = False
    _attr_name = "Luba Non-Work Drift"
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:clock-alert-outline"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._service = entry.runtime_data.garden.non_work
        self._gate = entry.runtime_data.garden.gate
        self._attr_unique_id = f"{DOMAIN}_luba_non_work_drift"
        self.entity_id = "binary_sensor.luba_non_work_drift"
        self._attr_device_info = luba_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))
        self.async_on_remove(self._gate.add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self._service.drift and not self._gate.is_holding

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        target_start, target_end = self._service.start, self._service.end
        cloud = self._service.cloud
        return {
            "target": (
                f"{target_start.isoformat('minutes')} - "
                f"{target_end.isoformat('minutes')}"
                if target_start and target_end
                else None
            ),
            "device": (
                f"{cloud.start.isoformat('minutes')} - {cloud.end.isoformat('minutes')}"
                if cloud
                else None
            ),
        }


class MowingShouldStartBinarySensor(BinarySensorEntity):
    """On when the mowing planner says: start Luba now."""

    _attr_has_entity_name = False
    _attr_name = "Mowing Should Start"
    _attr_should_poll = False
    _attr_icon = "mdi:robot-mower"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._service = entry.runtime_data.garden.mowing
        self._attr_unique_id = f"{DOMAIN}_mowing_should_start"
        self.entity_id = "binary_sensor.mowing_should_start"
        self._attr_device_info = luba_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        decision = self._service.decision
        return decision.should_start if decision else False


class GardenGrassWetBinarySensor(BinarySensorEntity):
    """On while it is currently raining (last observed wet state)."""

    _attr_has_entity_name = False
    _attr_name = "Garden Grass Wet"
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.MOISTURE
    _attr_icon = "mdi:weather-rainy"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._service = entry.runtime_data.garden.rain
        self._attr_unique_id = f"{DOMAIN}_garden_grass_wet"
        self.entity_id = "binary_sensor.garden_grass_wet"
        self._attr_device_info = luba_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self._service.currently_wet


class LubaResumeIntoWetBinarySensor(BinarySensorEntity):
    """On while the rain gate holds non-work open past the target (wet grass)."""

    _attr_has_entity_name = False
    _attr_name = "Luba Resume Into Wet"
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:weather-pouring"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._gate = entry.runtime_data.garden.gate
        self._attr_unique_id = f"{DOMAIN}_luba_resume_into_wet"
        self.entity_id = "binary_sensor.luba_resume_into_wet"
        self._attr_device_info = luba_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._gate.add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self._gate.is_holding

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        hold = self._gate.hold_until
        return {"effective_end": hold.isoformat() if hold else None}
