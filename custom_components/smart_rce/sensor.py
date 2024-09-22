"""Smart RCE Sensors."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Final

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfEnergy, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.dt import now as now_local

from . import SmartRceConfigEntry
from .const import DOMAIN
from .coordinator import RceData, SmartRceDataUpdateCoordinator
from .domain.ems import EmsDayPrices, find_charge_hours

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
    async_add_entities(
        [
            SmartRceStartChargeHourSensor(coordinator),
            SmartRceStartChargeHourTimeSensor(coordinator),
            SmartRceEndChargeHourSensor(coordinator),
            SmartRceCurrentPriceSensor(coordinator),
            SmartRceEndChargeHourTimeSensor(coordinator),
        ]
    )


class SmartRceStartChargeHourSensor(
    CoordinatorEntity[SmartRceDataUpdateCoordinator], SensorEntity
):
    _attr_has_entity_name = True
    _attr_name = "Start Charge Hour Today"
    _attr_native_unit_of_measurement = UnitOfTime.HOURS

    def __init__(
        self,
        coordinator: SmartRceDataUpdateCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._sensor_data: RceData = coordinator.data
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_start_charge_hour_today".lower()
        self._attr_device_info = coordinator.device_info
        self._start_charge_hour: float = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._handle_coordinator_update()
        _LOGGER.debug(
            "Setup of Smart RCE sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.data and self.coordinator.data.today:
            ems_prices: EmsDayPrices = find_charge_hours(self.coordinator.data.today)
            self._start_charge_hour = ems_prices.best_start_charge_hour()
            self.async_write_ha_state()
            _LOGGER.debug("Updated %s: %s", self._attr_name, self._start_charge_hour)

    @property
    def native_value(self) -> str | int | float | None:
        return self._start_charge_hour


class SmartRceStartChargeHourTimeSensor(
    CoordinatorEntity[SmartRceDataUpdateCoordinator], SensorEntity
):
    _attr_has_entity_name = True
    _attr_name = "Start Charge Hour Today Time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        coordinator: SmartRceDataUpdateCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._sensor_data: RceData = coordinator.data
        self._attr_unique_id = (
            f"{UNIQUE_ID_PREFIX}_start_charge_hour_today_time".lower()
        )
        self._attr_device_info = coordinator.device_info
        self._start_charge_hour_timestamp: datetime = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._handle_coordinator_update()
        _LOGGER.debug(
            "Setup of Smart RCE sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.data and self.coordinator.data.today:
            ems_prices: EmsDayPrices = find_charge_hours(self.coordinator.data.today)
            start_charge_hour = ems_prices.best_start_charge_hour()

            hour = int(start_charge_hour)
            minute = int(start_charge_hour * 60 % 60)

            now: datetime = now_local()
            self._start_charge_hour_timestamp = now.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            self.async_write_ha_state()

            _LOGGER.debug(
                "Updated %s: %s", self._attr_name, self._start_charge_hour_timestamp
            )

    @property
    def native_value(self) -> datetime | None:
        return self._start_charge_hour_timestamp


class SmartRceEndChargeHourSensor(
    CoordinatorEntity[SmartRceDataUpdateCoordinator], SensorEntity
):
    _attr_has_entity_name = True
    _attr_name = "End Charge Hour Today"
    _attr_native_unit_of_measurement = UnitOfTime.HOURS

    def __init__(
        self,
        coordinator: SmartRceDataUpdateCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._sensor_data: RceData = coordinator.data
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_end_charge_hour_today".lower()
        self._attr_device_info = coordinator.device_info
        self._end_charge_hour: float = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._handle_coordinator_update()
        _LOGGER.debug(
            "Setup of Smart RCE sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.data and self.coordinator.data.today:
            ems_prices: EmsDayPrices = find_charge_hours(self.coordinator.data.today)
            self._end_charge_hour = ems_prices.end_start_charge_hour()
            self.async_write_ha_state()
            _LOGGER.debug("Updated %s: %s", self._attr_name, self._end_charge_hour)

    @property
    def native_value(self) -> str | int | float | None:
        return self._end_charge_hour


class SmartRceEndChargeHourTimeSensor(
    CoordinatorEntity[SmartRceDataUpdateCoordinator], SensorEntity
):
    _attr_has_entity_name = True
    _attr_name = "End Charge Hour Today Time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        coordinator: SmartRceDataUpdateCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._sensor_data: RceData = coordinator.data
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_end_charge_hour_today_time".lower()
        self._attr_device_info = coordinator.device_info
        self._end_charge_hour_timestamp: datetime = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._handle_coordinator_update()
        _LOGGER.debug(
            "Setup of Smart RCE sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.data and self.coordinator.data.today:
            ems_prices: EmsDayPrices = find_charge_hours(self.coordinator.data.today)
            end_charge_hour = ems_prices.end_start_charge_hour()

            hour = int(end_charge_hour)
            minute = int(end_charge_hour * 60 % 60)

            now: datetime = now_local()
            self._end_charge_hour_timestamp = now.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            self.async_write_ha_state()

            _LOGGER.debug(
                "Updated %s: %s", self._attr_name, self._end_charge_hour_timestamp
            )

    @property
    def native_value(self) -> datetime | None:
        return self._end_charge_hour_timestamp


class SmartRceCurrentPriceSensor(
    CoordinatorEntity[SmartRceDataUpdateCoordinator], SensorEntity
):
    _attr_has_entity_name = True
    _attr_name = "Current Price"
    _attr_native_unit_of_measurement = f"{CURRENCY_PLN}/{UnitOfEnergy.MEGA_WATT_HOUR}"
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(
        self,
        coordinator: SmartRceDataUpdateCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._sensor_data: RceData = coordinator.data
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_current_price".lower()
        self._attr_device_info = coordinator.device_info
        self._current_price: float = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Update 'state' value in hour changes
        self.async_on_remove(
            async_track_time_change(
                self.hass, self.update_current_price, minute=0, second=0
            )
        )
        self._handle_coordinator_update()
        _LOGGER.debug(
            "Setup of RCE Smart sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self.update_current_price(now_local())

    @callback
    def update_current_price(self, now: datetime) -> None:
        if self.coordinator.data and self.coordinator.data.today:
            self._current_price = self.coordinator.data.today.prices[now.hour]["price"]
            _LOGGER.debug("Updated current price to: %s", self._current_price)
            self.async_write_ha_state()

    @property
    def native_value(self) -> str | int | float | None:
        return self._current_price
