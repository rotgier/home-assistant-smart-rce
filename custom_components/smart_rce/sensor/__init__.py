"""Smart RCE sensor platform — orchestrator + setup_entry.

Sensors split per concept (4 modules):
- rce_sensor: SmartRceSensor + SENSOR_DESCRIPTIONS (RCE prices + charge/discharge slots)
- pv_forecast_sensor: PvForecastSensor + build_pv_forecast_sensors factory
- weather_history_sensor: WeatherForecastHistorySensor
- ems_sensor: EmsSensor + EMS_SENSOR_DESCRIPTIONS

Each module self-contained: entity class + description schema + tuple
+ concept-specific helpers. Common helpers live in `_helpers.py`.
"""

from __future__ import annotations

from typing import Final

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .. import SmartRceConfigEntry
from .ems_sensor import EMS_SENSOR_DESCRIPTIONS, EmsSensor
from .pv_forecast_sensor import build_pv_forecast_sensors
from .rce_sensor import SENSOR_DESCRIPTIONS, SmartRceSensor
from .weather_history_sensor import WeatherForecastHistorySensor

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

    sensors.extend(build_pv_forecast_sensors(pv_forecast, coordinator))
    sensors.append(
        WeatherForecastHistorySensor(weather_history, weather_listener, coordinator)
    )

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
