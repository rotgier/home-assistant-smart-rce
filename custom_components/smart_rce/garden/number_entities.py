"""Garden number entities — user-tunable planner policies.

`number.garden_dry_out_hours` is the dry-out policy: how many hours after rain
ends the grass is considered dry enough to mow (backed by `RainService` /
`RainRepository`, feeds the planner's `dry_at` floor).
`number.mowing_fresh_start_battery` is the SoC threshold above which a fresh
program is dispatched (backed by `MowingPlannerService` / `MowingPolicyRepository`,
Store-persisted — a domain policy, like dry-out hours).
`number.mowing_park_minutes` is a UI-input parameter for the park button
(RestoreNumber — not domain state). Top-level `number.py` aggregates these via
`build_numbers`, so garden owns its presentation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.smart_rce.const import DOMAIN
from custom_components.smart_rce.garden.garden_device import luba_device_info
from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
    RestoreNumber,
)

if TYPE_CHECKING:
    from custom_components.smart_rce import SmartRceConfigEntry


def build_numbers(entry: SmartRceConfigEntry) -> list[NumberEntity]:
    """Garden number entities for top-level `number.py` to add."""
    return [
        GardenDryOutHoursNumber(entry),
        MowingFreshStartBatteryNumber(entry),
        MowingParkMinutesNumber(entry),
    ]


class GardenDryOutHoursNumber(NumberEntity):
    """Hours after rain ends before the grass is dry enough to mow."""

    _attr_has_entity_name = False
    _attr_name = "Garden Dry-Out Hours"
    _attr_should_poll = False
    _attr_icon = "mdi:weather-sunny-alert"
    _attr_native_min_value = 0
    _attr_native_max_value = 24
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "h"
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._service = entry.runtime_data.garden.rain
        self._attr_unique_id = f"{DOMAIN}_garden_dry_out_hours"
        self.entity_id = "number.garden_dry_out_hours"
        self._attr_device_info = luba_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> float:
        return self._service.dry_hours

    async def async_set_native_value(self, value: float) -> None:
        await self._service.set_dry_hours(value)


class MowingFreshStartBatteryNumber(NumberEntity):
    """SoC threshold above which a fresh program is dispatched.

    Backed by `MowingPlannerService` / `MowingPolicyRepository` (Store) — a domain
    policy that feeds the planner decision, so it persists via Store (like
    `garden_dry_out_hours`), NOT RestoreNumber. Below this SoC a fresh start waits
    and charges (a wide window); above it the planner GOes at the window open.
    """

    _attr_has_entity_name = False
    _attr_name = "Mowing Fresh-Start Battery"
    _attr_should_poll = False
    _attr_icon = "mdi:battery-charging-90"
    _attr_native_min_value = 30
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"
    _attr_device_class = NumberDeviceClass.BATTERY
    _attr_mode = NumberMode.SLIDER

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._service = entry.runtime_data.garden.mowing
        self._attr_unique_id = f"{DOMAIN}_mowing_fresh_start_battery"
        self.entity_id = "number.mowing_fresh_start_battery"
        self._attr_device_info = luba_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> float:
        return self._service.fresh_start_battery

    async def async_set_native_value(self, value: float) -> None:
        self._service.set_fresh_start_battery(int(value))
        self.async_write_ha_state()


class MowingParkMinutesNumber(RestoreNumber):
    """How long `button.mowing_park` keeps Luba docked (UI input parameter).

    A pure UI parameter (not a domain policy), so RestoreNumber holds its own
    value across restarts. The park button reads it at press time.
    """

    _attr_has_entity_name = False
    _attr_name = "Mowing Park Minutes"
    _attr_should_poll = False
    _attr_icon = "mdi:timer-pause-outline"
    _attr_native_min_value = 5
    _attr_native_max_value = 240
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "min"
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._attr_unique_id = f"{DOMAIN}_mowing_park_minutes"
        self.entity_id = "number.mowing_park_minutes"
        self._attr_device_info = luba_device_info(entry)
        self._attr_native_value = 30.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = last.native_value

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
