"""Smart RCE sensor platform — orchestrator + setup_entry.

Sensors split per concept (4 modules):
- rce_sensor: SmartRceSensor + SENSOR_DESCRIPTIONS (RCE prices + charge/discharge slots)
- energy_balance_sensor: EnergyBalanceSensor + ENERGY_BALANCE_DESCRIPTIONS (PV kWh + Target SOC % + bucket projections)
- weather_history_sensor: WeatherForecastHistorySensor
- ems_sensor: EmsSensor + EMS_SENSOR_DESCRIPTIONS

Each module self-contained: entity class + description schema + tuple
+ concept-specific helpers. Common helpers live in `_helpers.py`.
"""

from __future__ import annotations

from typing import Final

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .. import SmartRceConfigEntry
from ..garden.sensor_entities import build_sensors as build_garden_sensors
from .ems_sensor import EMS_SENSOR_DESCRIPTIONS, EmsSensor
from .energy_balance_sensor import ENERGY_BALANCE_DESCRIPTIONS, EnergyBalanceSensor
from .rce_sensor import SENSOR_DESCRIPTIONS, SmartRceSensor
from .target_soc_matrix_sensor import SmartRceTargetSocMatrixSensor
from .weather_history_sensor import WeatherForecastHistorySensor
from .weather_table_sensor import (
    SmartRceWeatherTableSensor,
    SmartRceWeatherTableSnapshotSensor,
)

PARALLEL_UPDATES: Final = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartRceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add Smart RCE sensors."""
    coordinator = entry.runtime_data.rce_coordinator
    ems = entry.runtime_data.ems
    pv_forecast = entry.runtime_data.pv_forecast
    weather_history = entry.runtime_data.weather_forecast_history
    weather_listener = entry.runtime_data.weather_listener

    sensors: list[SensorEntity] = [
        SmartRceSensor(coordinator, ems, description)
        for description in SENSOR_DESCRIPTIONS
    ]

    sensors.extend(
        EnergyBalanceSensor(pv_forecast, coordinator, description)
        for description in ENERGY_BALANCE_DESCRIPTIONS
    )
    sensors.append(
        WeatherForecastHistorySensor(weather_history, weather_listener, coordinator)
    )
    sensors.append(
        SmartRceWeatherTableSensor(
            hass,
            entry.runtime_data.weather_table_service,
            weather_listener,
            coordinator,
        )
    )
    sensors.append(
        SmartRceWeatherTableSnapshotSensor(
            hass,
            entry.runtime_data.weather_table_service,
            weather_listener,
            coordinator,
        )
    )
    sensors.append(
        SmartRceTargetSocMatrixSensor(
            hass,
            entry.runtime_data.target_soc_matrix_service,
            weather_listener,
            pv_forecast,
            coordinator,
        )
    )

    sensors.extend(
        EmsSensor(entry.entry_id, ems, description)
        for description in EMS_SENSOR_DESCRIPTIONS
    )

    sensors.extend(build_garden_sensors(entry))

    async_add_entities(sensors)
