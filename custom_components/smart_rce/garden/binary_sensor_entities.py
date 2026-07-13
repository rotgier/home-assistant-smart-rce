"""Garden binary sensors — mowing should-start + grass-wet + mowing hold.

(The old `binary_sensor.luba_non_work_drift` was folded into
`sensor.mowing_non_work_status`, which VERIFIES device-vs-expected and names the
reason — see `sensor_entities.py`.)

`binary_sensor.mowing_should_start` mirrors `PlannerDecision.should_start`
(garden 2b) — the trigger entity for the "Puść Lubę" alert automation.

`binary_sensor.mowing_hold` is on while the mowing hold has overridden the
non-work window (grass not yet dry) — the trigger entity for the "Luba
wstrzymana" alert automation (2d).

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
        MowingShouldStartBinarySensor(entry),
        GardenGrassWetBinarySensor(entry),
        MowingHoldBinarySensor(entry),
    ]


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


class MowingHoldBinarySensor(BinarySensorEntity):
    """On while the mowing hold overrides the device non-work window (wet grass).

    Covers both shapes: the morning-boundary end-extension and the working-hours
    hold `[now, dry_at]` that preempts the charge-complete auto-resume.
    """

    _attr_has_entity_name = False
    _attr_name = "Mowing Hold"
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:weather-pouring"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._hold = entry.runtime_data.garden.hold
        self._attr_unique_id = f"{DOMAIN}_mowing_hold"
        self.entity_id = "binary_sensor.mowing_hold"
        self._attr_device_info = luba_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._hold.add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self._hold.is_holding

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        override = self._hold.override
        since, until = self._hold.manual_since, self._hold.manual_until
        return {
            # Raw end kept for the notify automation template.
            "effective_end": override.end.isoformat() if override else None,
            # Ranges for the dashboard (mirror the device "HH:MM - HH:MM" shape).
            "effective_window": (
                f"{override.start:%H:%M} - {override.end:%H:%M}" if override else None
            ),
            "manual_window": (
                f"{since:%H:%M} - {until:%H:%M}" if since and until else None
            ),
            "manual_parked": self._hold.is_manual_parked,
        }
