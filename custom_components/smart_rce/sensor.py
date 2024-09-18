"""Smart RCE Sensors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from statistics import mean
from typing import Final

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.components.weather import DOMAIN as WEATHER, WeatherEntity
from homeassistant.const import ATTR_ENTITY_ID, UnitOfEnergy, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    Event,
    async_track_entity_registry_updated_event,
    async_track_time_change,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.dt import now as now_local
from homeassistant.util.json import JsonValueType

from . import SmartRceConfigEntry
from .const import DOMAIN
from .coordinator import RceData, SmartRceDataUpdateCoordinator
from .rce_api import RceDayPrices

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
    async_add_entities([SmartRceCurrentPriceSensor(entry.runtime_data.rce_coordinator)])


def find_start_charge_hour(day_prices: RceDayPrices) -> int:
    """Find start charge hour."""
    prices: list[float] = [item["price"] for item in day_prices.prices]
    consecutive_prices: list[HourPrices] = calculate_consecutive_prices(prices)
    min_consecutive_prices_3_hours = sorted(
        consecutive_prices, key=lambda x: x.mean_price_consecutive_3_hours
    )
    min_consecutive_prices_4_hours = sorted(
        consecutive_prices, key=lambda x: x.mean_price_consecutive_4_hours
    )
    min_consecutive_prices_5_hours = sorted(
        consecutive_prices, key=lambda x: x.mean_price_consecutive_5_hours
    )
    return min_consecutive_prices_3_hours[0].hour


@dataclass(kw_only=True)
class HourPrices:  # noqa: D101
    hour: int
    price: float
    mean_price_consecutive_3_hours: float = float("inf")
    mean_price_consecutive_4_hours: float = float("inf")
    mean_price_consecutive_5_hours: float = float("inf")


def calculate_consecutive_prices(prices: list[float]) -> list[HourPrices]:
    consecutive_prices: list[HourPrices] = [
        HourPrices(hour=hour, price=price) for hour, price in enumerate(prices)
    ]
    for i in range(6, 14):
        consecutive_prices[i].mean_price_consecutive_3_hours = mean(prices[i : i + 3])
        consecutive_prices[i].mean_price_consecutive_4_hours = mean(prices[i : i + 4])
        consecutive_prices[i].mean_price_consecutive_5_hours = mean(prices[i : i + 5])
    return consecutive_prices


class SmartRceStartChargeHourSensor(
    CoordinatorEntity[SmartRceDataUpdateCoordinator], SensorEntity
):
    """Define an Smart RCE entity."""

    _attr_has_entity_name = True
    _attr_name = "Start Charge Hour"
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    # entity_description: AccuWeatherSensorDescription

    def __init__(
        self,
        coordinator: SmartRceDataUpdateCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)

        self._sensor_data: RceData = coordinator.data
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_start_charge_hour".lower()
        self._attr_device_info = coordinator.device_info
        self._start_charge_hour: int = None

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        _LOGGER.debug(
            "Setup of Smart RCE sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update the sensor state, by selecting the best hour to start charging."""
        if self.coordinator.data and self.coordinator.data.today:
            now = now_local()
            self._current_price = self.coordinator.data.today.prices[now.hour]["price"]
            self.async_write_ha_state()

    @property
    def native_value(self) -> str | int | float | None:
        """Return the state."""
        return self._current_price


class SmartRceCurrentPriceSensor(
    CoordinatorEntity[SmartRceDataUpdateCoordinator], SensorEntity
):
    """Define an Smart RCE entity."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_has_entity_name = True
    _attr_name = "Current Price"
    _attr_native_unit_of_measurement = f"{CURRENCY_PLN}/{UnitOfEnergy.MEGA_WATT_HOUR}"
    # entity_description: AccuWeatherSensorDescription

    def __init__(
        self,
        coordinator: SmartRceDataUpdateCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)

        self._sensor_data: RceData = coordinator.data
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_current_price".lower()
        self._attr_device_info = coordinator.device_info
        self._current_price: float = None

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        # Update 'state' value in hour changes
        self.async_on_remove(
            async_track_time_change(
                self.hass, self.update_current_price, minute="*", second=0
            )
        )
        # self.async_on_remove(
        #     async_track_entity_registry_updated_event(
        #         self.hass,
        #         "weather.wetteronline_pogoda_i_radar",
        #         self.async_registry_updated,
        #     )
        # )
        # component: EntityComponent[WeatherEntity] = self.hass.data[WEATHER]
        # forecast_type: str = "hourly"
        # entity = component.get_entity("weather.wetteronline_pogoda_i_radar")
        # entity = component.get_entity("wetteronline_pogoda_i_radar")

        # weather = await self.hass.services.async_call(
        #     WEATHER,
        #     "get_forecasts",
        #     {"type": "hourly"},
        #     blocking=True,
        #     target={ATTR_ENTITY_ID: "weather.wetteronline_pogoda_i_radar"},
        #     return_response=True,
        # )
        _LOGGER.debug(
            "Setup of RCE Smart sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle data update."""
        self.update_current_price(now_local())

    @callback
    def async_registry_updated(
        self,
        event: Event[er.EventEntityRegistryUpdatedData],
    ) -> None:
        component: EntityComponent[WeatherEntity] = self.hass.data[WEATHER]
        forecast_type: str = "hourly"
        entity = component.get_entity("weather.wetteronline_pogoda_i_radar")

        entity.async_subscribe_forecast(forecast_type, self.forecast_listener)

        # weather = await self.hass.services.async_call(
        #     WEATHER,
        #     "get_forecasts",
        #     {"type": "hourly"},
        #     blocking=True,
        #     target={ATTR_ENTITY_ID: "weather.wetteronline_pogoda_i_radar"},
        #     return_response=True,
        # )

    @callback
    def forecast_listener(forecast: list[JsonValueType] | None) -> None:
        wow = 2
        return None

    @callback
    def update_current_price(self, now: datetime) -> None:
        """Update the sensor state, by selecting the current price for this hour."""
        if self.coordinator.data and self.coordinator.data.today:
            self._current_price = self.coordinator.data.today.prices[now.hour]["price"]
            self.async_write_ha_state()

    @property
    def native_value(self) -> str | int | float | None:
        """Return the state."""
        return self._current_price



