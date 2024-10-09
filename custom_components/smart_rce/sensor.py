"""Smart RCE Sensors."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Final

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfEnergy, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SmartRceConfigEntry
from .const import DOMAIN
from .coordinator import SmartRceDataUpdateCoordinator
from .domain.ems import Ems

CURRENCY_PLN: Final = "zł"
UNIQUE_ID_PREFIX = DOMAIN

PARALLEL_UPDATES = 1


_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartRceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add Smart RCE sensors."""
    coordinator = entry.runtime_data.rce_coordinator
    ems = entry.runtime_data.ems
    async_add_entities(
        [
            SmartRceStartChargeHourSensor(coordinator, ems),
            SmartRceStartChargeHourTimeSensor(coordinator, ems),
            SmartRceEndChargeHourSensor(coordinator, ems),
            SmartRceEndChargeHourTimeSensor(coordinator, ems),
            SmartRceCurrentPriceSensor(coordinator, ems),
        ]
    )


class SmartRceSensor(CoordinatorEntity[SmartRceDataUpdateCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: SmartRceDataUpdateCoordinator, ems: Ems) -> None:
        super().__init__(coordinator)
        name_as_id = self._attr_name.lower().replace(" ", "_")
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_{name_as_id}"
        self._attr_device_info = coordinator.device_info
        self.ems: Ems = ems

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def listener() -> None:
            self.async_write_ha_state()

        remove_listener = self.ems.async_add_listener(listener)

        @callback
        def remove_listener_as_callback() -> None:
            remove_listener()

        self.async_on_remove(remove_listener_as_callback)

        self._handle_coordinator_update()
        _LOGGER.debug(
            "Setup of RCE Smart sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )


class SmartRceStartChargeHourSensor(SmartRceSensor):
    _attr_name = "Start Charge Hour Today"
    _attr_native_unit_of_measurement = UnitOfTime.HOURS

    @property
    def native_value(self) -> str | int | float | None:
        return self.ems.today.start_charge_hour


class SmartRceStartChargeHourTimeSensor(SmartRceSensor):
    _attr_name = "Start Charge Hour Today Time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        return self.ems.today.start_charge_hour_datetime


class SmartRceEndChargeHourSensor(SmartRceSensor):
    _attr_name = "End Charge Hour Today"
    _attr_native_unit_of_measurement = UnitOfTime.HOURS

    @property
    def native_value(self) -> str | int | float | None:
        return self.ems.today.end_charge_hour


class SmartRceEndChargeHourTimeSensor(SmartRceSensor):
    _attr_name = "End Charge Hour Today Time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        return self.ems.today.end_charge_hour_datetime


class SmartRceCurrentPriceSensor(SmartRceSensor):
    _attr_name = "Current Price"
    _attr_native_unit_of_measurement = f"{CURRENCY_PLN}/{UnitOfEnergy.MEGA_WATT_HOUR}"
    _attr_device_class = SensorDeviceClass.MONETARY

    @property
    def native_value(self) -> str | int | float | None:
        return self.ems.current_price
