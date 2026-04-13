"""Smart RCE Sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
import logging
from typing import Any, Final

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SmartRceConfigEntry
from .const import DOMAIN
from .coordinator import SmartRceDataUpdateCoordinator
from .domain.ems import Ems
from .pv_forecast_coordinator import PvForecastCoordinator
from .weather_forecast_history import WeatherForecastHistory

CURRENCY_PLN: Final = "zł"
UNIQUE_ID_PREFIX = DOMAIN

PARALLEL_UPDATES = 1


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=False, kw_only=True)
class SmartRceSensorDescription(SensorEntityDescription):
    key: str = field(init=False)
    value_fn: Callable[[Ems], str | int | float | datetime | None]
    attr_fn: Callable[[dict[str, Any]], dict[str, Any]] = lambda _: {}

    def __post_init__(self):
        self.key = self.name.lower().replace(" ", "_")


def _avg_price(ems: Ems, day: str) -> float | None:
    rce_data = ems.rce_data
    if not rce_data:
        return None
    day_prices = rce_data.today if day == "today" else rce_data.tomorrow
    if not day_prices or not day_prices.prices:
        return None
    prices = [p["price"] for p in day_prices.prices]
    return round(sum(prices) / len(prices), 2)


def _prices_attr(ems: Ems, day: str) -> dict[str, Any]:
    rce_data = ems.rce_data
    if not rce_data:
        return {}
    day_prices = rce_data.today if day == "today" else rce_data.tomorrow
    if not day_prices or not day_prices.prices:
        return {}
    return {
        "prices": [
            {
                "datetime": p["datetime"].isoformat(),
                "price": p["price"],
            }
            for p in day_prices.prices
        ]
    }


SENSOR_DESCRIPTIONS: tuple[SmartRceSensorDescription, ...] = (
    SmartRceSensorDescription(
        name="Current Price",
        native_unit_of_measurement=f"{CURRENCY_PLN}/{UnitOfEnergy.MEGA_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.current_price,
    ),
    SmartRceSensorDescription(
        name="Prices Today",
        native_unit_of_measurement=f"{CURRENCY_PLN}/{UnitOfEnergy.MEGA_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: _avg_price(ems, "today"),
        attr_fn=lambda ems: _prices_attr(ems, "today"),
    ),
    SmartRceSensorDescription(
        name="Prices Tomorrow",
        native_unit_of_measurement=f"{CURRENCY_PLN}/{UnitOfEnergy.MEGA_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: _avg_price(ems, "tomorrow"),
        attr_fn=lambda ems: _prices_attr(ems, "tomorrow"),
    ),
    ####
    #### TODAY
    ####
    SmartRceSensorDescription(
        name="Start Charge Hour Today",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.today.start_charge_hour,
    ),
    SmartRceSensorDescription(
        name="Start Charge Hour Today Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.today.start_charge_hour_datetime,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Today",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.today.end_charge_hour,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Today Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.today.end_charge_hour_datetime,
    ),
    ####
    #### TOMORROW
    ####
    SmartRceSensorDescription(
        name="Start Charge Hour Tomorrow",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.tomorrow.start_charge_hour,
    ),
    SmartRceSensorDescription(
        name="Start Charge Hour Tomorrow Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.tomorrow.start_charge_hour_datetime,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Tomorrow",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.tomorrow.end_charge_hour,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Tomorrow Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.tomorrow.end_charge_hour_datetime,
    ),
)


EMS_UNIQUE_ID_PREFIX = "ems"


@dataclass(frozen=False, kw_only=True)
class EmsSensorDescription(SensorEntityDescription):
    key: str = field(init=False)
    value_fn: Callable[[Ems], str | int | float | None]

    def __post_init__(self):
        self.key = self.name.lower().replace(" ", "_")


EMS_SENSOR_DESCRIPTIONS: tuple[EmsSensorDescription, ...] = (
    EmsSensorDescription(
        name="Heater Budget",
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.water_heater.balanced_heater_budget,
        icon="mdi:lightning-bolt",
    ),
    EmsSensorDescription(
        name="Balanced Baseline",
        value_fn=lambda ems: ems.water_heater.balanced_baseline,
        icon="mdi:heating-coil",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartRceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add Smart RCE sensors."""
    coordinator = entry.runtime_data.rce_coordinator
    ems = entry.runtime_data.ems
    pv_forecast = entry.runtime_data.pv_forecast_coordinator

    sensors: list[SensorEntity] = [
        SmartRceSensor(coordinator, ems, description)
        for description in SENSOR_DESCRIPTIONS
    ]

    weather_history = entry.runtime_data.weather_forecast_history
    weather_coordinator = entry.runtime_data.weather_coordinator

    sensors.extend(
        [
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Weather Adjusted PV At 6",
                lambda pv: pv.adjusted_at_6.total_kwh if pv.adjusted_at_6 else None,
                lambda pv: _pv_forecast_attrs(pv.adjusted_at_6),
            ),
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Weather Adjusted PV Live",
                lambda pv: pv.adjusted_live.total_kwh if pv.adjusted_live else None,
                lambda pv: _pv_forecast_attrs(pv.adjusted_live),
            ),
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Target Battery SOC",
                lambda pv: pv.target_soc,
                lambda _pv: {},
                unit="%",
            ),
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Target Battery SOC Live",
                lambda pv: pv.target_soc_live,
                lambda _pv: {},
                unit="%",
            ),
            WeatherForecastHistorySensor(
                weather_history,
                weather_coordinator,
                coordinator,
            ),
        ]
    )

    from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

    ems_device_info = DeviceInfo(
        name="EMS",
        identifiers={("ems", entry.entry_id)},
        entry_type=DeviceEntryType.SERVICE,
    )
    sensors.extend(
        EmsSensor(ems_device_info, ems, description)
        for description in EMS_SENSOR_DESCRIPTIONS
    )

    async_add_entities(sensors)


def _pv_forecast_attrs(forecast) -> dict[str, Any]:
    if not forecast:
        return {}
    return {
        "forecast": [
            {
                "period_start": p.period_start,
                "pv_estimate_adjusted": p.pv_estimate_adjusted,
            }
            for p in forecast.forecast
        ]
    }


class SmartRceSensor(CoordinatorEntity[SmartRceDataUpdateCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: SmartRceSensorDescription

    def __init__(
        self,
        coordinator: SmartRceDataUpdateCoordinator,
        ems: Ems,
        description: SmartRceSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.ems: Ems = ems
        self.entity_description = description
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_{description.key}"
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def listener() -> None:
            self.async_write_ha_state()

        remove_listener = self.ems.async_add_listener(listener)
        setattr(remove_listener, "_hass_callback", True)
        self.async_on_remove(remove_listener)

        self._handle_coordinator_update()
        _LOGGER.debug(
            "Setup of RCE Smart sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @property
    def native_value(self) -> str | int | float | datetime | None:
        return self.entity_description.value_fn(self.ems)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.entity_description.attr_fn(self.ems)


class PvForecastSensor(SensorEntity):
    """Sensor for weather-adjusted PV forecast data."""

    _attr_has_entity_name = True

    def __init__(
        self,
        pv_forecast: PvForecastCoordinator,
        rce_coordinator: SmartRceDataUpdateCoordinator,
        name: str,
        value_fn: Callable[[PvForecastCoordinator], float | int | None],
        attr_fn: Callable[[PvForecastCoordinator], dict[str, Any]],
        unit: str | None = None,
    ) -> None:
        self._pv_forecast = pv_forecast
        self._value_fn = value_fn
        self._attr_fn = attr_fn
        self._attr_name = name
        key = name.lower().replace(" ", "_")
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_{key}"
        self._attr_device_info = rce_coordinator.device_info
        if unit:
            self._attr_native_unit_of_measurement = unit
        else:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_state_class = SensorStateClass.MEASUREMENT

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def listener() -> None:
            self.async_write_ha_state()

        remove_listener = self._pv_forecast.async_add_listener(listener)
        setattr(remove_listener, "_hass_callback", True)
        self.async_on_remove(remove_listener)

        _LOGGER.debug(
            "Setup of PV Forecast sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @property
    def native_value(self) -> float | int | None:
        return self._value_fn(self._pv_forecast)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attr_fn(self._pv_forecast)


class WeatherForecastHistorySensor(RestoreSensor):
    """Sensor tracking hourly weather forecast conditions throughout the day.

    State changes once per hour (e.g. "07:00 cloudy") — recorder saves snapshot.
    Attribute 'hours' updates every ~5 min from wetteronline forecast.
    """

    _attr_has_entity_name = True
    _attr_name = "Weather Forecast History"

    def __init__(
        self,
        weather_history: WeatherForecastHistory,
        weather_coordinator: Any,
        rce_coordinator: SmartRceDataUpdateCoordinator,
    ) -> None:
        self._weather_history = weather_history
        self._weather_coordinator = weather_coordinator
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_weather_forecast_history"
        self._attr_device_info = rce_coordinator.device_info
        self._attr_native_value: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Restore from RestoreSensor
        last_sensor_data = await self.async_get_last_sensor_data()
        if last_sensor_data and last_sensor_data.native_value:
            self._attr_native_value = last_sensor_data.native_value

        last_state = await self.async_get_last_state()
        if last_state:
            hours_attr = last_state.attributes.get("hours")
            if hours_attr:
                from homeassistant.util.dt import now as now_local

                self._weather_history.restore(hours_attr, now_local().date())

        # Listen for weather updates
        @callback
        def on_weather_update() -> None:
            self._handle_weather_update()

        remove_listener = self._weather_coordinator.async_add_listener(
            on_weather_update
        )
        setattr(remove_listener, "_hass_callback", True)
        self.async_on_remove(remove_listener)

        _LOGGER.debug(
            "Setup of Weather Forecast History sensor %s (unique_id: %s)",
            self.entity_id,
            self._attr_unique_id,
        )

    @callback
    def _handle_weather_update(self) -> None:
        """Handle weather forecast update."""
        from homeassistant.util.dt import now as now_local

        now = now_local()
        today = now.date()

        # Update hours from forecast
        self._weather_history.update_from_forecast(
            self._weather_coordinator.forecast_hourly, today
        )

        # Check if state should change (new hour)
        current_hour_str = f"{now.hour:02d}:00"
        current_value = self._attr_native_value or ""
        if not current_value.startswith(current_hour_str):
            condition = self._weather_history.get_condition(now.hour)
            self._attr_native_value = f"{current_hour_str} {condition}"

        self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        return self._attr_native_value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"hours": self._weather_history.hours_attribute}


class EmsSensor(SensorEntity):
    """EMS diagnostic sensor (heater_budget, balanced_baseline)."""

    _attr_has_entity_name = True
    entity_description: EmsSensorDescription

    def __init__(
        self,
        device_info,
        ems: Ems,
        description: EmsSensorDescription,
    ) -> None:
        self._attr_device_info = device_info
        self.ems: Ems = ems
        self.entity_description = description
        self._attr_unique_id = f"{EMS_UNIQUE_ID_PREFIX}_{description.key}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def listener() -> None:
            self.async_write_ha_state()

        remove_listener = self.ems.async_add_listener(listener)
        setattr(remove_listener, "_hass_callback", True)
        self.async_on_remove(remove_listener)

        self.async_write_ha_state()
        _LOGGER.debug(
            "Setup of EMS sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @cached_property
    def should_poll(self) -> bool:
        return False

    @property
    def native_value(self) -> str | int | float | None:
        return self.entity_description.value_fn(self.ems)
