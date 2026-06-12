"""Garden binary sensors — non-work drift + mowing should-start.

`binary_sensor.luba_non_work_drift` turns on when the mammotion non-work
sensor reports a different window than the user-set target (observe-first:
the alert automation notifies; nothing is written to the device). Off when
either side is unknown.

`binary_sensor.mowing_should_start` mirrors `PlannerDecision.should_start`
(garden 2b) — the trigger entity for the "Puść Lubę" alert automation.

Top-level `binary_sensor.py` aggregates these via `build_binary_sensors`,
so garden owns its presentation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.smart_rce.const import DOMAIN
from custom_components.smart_rce.ems_device import ems_device_info
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)

if TYPE_CHECKING:
    from custom_components.smart_rce import SmartRceConfigEntry


def build_binary_sensors(entry: SmartRceConfigEntry) -> list[BinarySensorEntity]:
    """Garden binary sensors for top-level `binary_sensor.py` to add."""
    return [LubaNonWorkDriftBinarySensor(entry), MowingShouldStartBinarySensor(entry)]


class LubaNonWorkDriftBinarySensor(BinarySensorEntity):
    """On when the device's non-work window differs from the HA target."""

    _attr_has_entity_name = False
    _attr_name = "Luba Non-Work Drift"
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:clock-alert-outline"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._service = entry.runtime_data.garden.non_work
        self._attr_unique_id = f"{DOMAIN}_luba_non_work_drift"
        self.entity_id = "binary_sensor.luba_non_work_drift"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self._service.drift

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
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        decision = self._service.decision
        return decision.should_start if decision else False
