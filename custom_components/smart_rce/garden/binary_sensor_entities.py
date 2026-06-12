"""Garden binary sensors — non-work drift (cloud vs HA target).

`binary_sensor.luba_non_work_drift` turns on when the mammotion non-work
sensor reports a different window than the user-set target (observe-first:
the alert automation notifies; nothing is written to the device). Off when
either side is unknown. Top-level `binary_sensor.py` aggregates these via
`build_binary_sensors`, so garden owns its presentation.
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
    return [LubaNonWorkDriftBinarySensor(entry)]


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
