"""The Smart RCE component."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module, reload
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .adapter import create_ems
from .coordinator import SmartRceDataUpdateCoordinator
from .domain.ems import Ems
from .infrastructure.rce_api import RceApi
from .pv_forecast_coordinator import PvForecastCoordinator
from .weather_forecast_history import WeatherForecastHistory
from .weather_listener import WeatherListenerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


@dataclass
class SmartRceData:
    """Smart Rce Data."""

    ems: Ems
    rce_coordinator: SmartRceDataUpdateCoordinator
    weather_coordinator: WeatherListenerCoordinator
    pv_forecast_coordinator: PvForecastCoordinator
    weather_forecast_history: WeatherForecastHistory


type SmartRceConfigEntry = ConfigEntry[SmartRceData]


async def async_setup_entry(hass: HomeAssistant, entry: SmartRceConfigEntry) -> bool:
    """Set up Smart RCE as config entry."""
    _LOGGER.debug("async_setup_entry")

    websession = async_get_clientsession(hass)
    rceApi = RceApi(websession)
    ems: Ems = await create_ems(hass, entry)
    rce_coordinator = SmartRceDataUpdateCoordinator(hass, rceApi, ems, entry)
    weather_coordinator = WeatherListenerCoordinator(hass, entry)

    weather_forecast_history = WeatherForecastHistory()
    pv_forecast_coordinator = PvForecastCoordinator(
        hass, weather_coordinator, weather_forecast_history
    )

    await rce_coordinator.async_config_entry_first_refresh()

    entry.runtime_data = SmartRceData(
        ems,
        rce_coordinator,
        weather_coordinator,
        pv_forecast_coordinator,
        weather_forecast_history,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    await pv_forecast_coordinator.async_start()

    return True


def live_reload():
    reload(import_module("custom_components.smart_rce.const"))
    reload(import_module("custom_components.smart_rce.infrastructure.rce_api"))
    reload(import_module("custom_components.smart_rce.infrastructure"))
    reload(import_module("custom_components.smart_rce.domain.rce"))
    reload(import_module("custom_components.smart_rce.domain.input_state"))
    reload(import_module("custom_components.smart_rce.domain.battery"))
    reload(import_module("custom_components.smart_rce.domain.water_heater"))
    reload(import_module("custom_components.smart_rce.domain.grid_export_positive"))
    reload(import_module("custom_components.smart_rce.domain.grid_export_negative"))
    reload(import_module("custom_components.smart_rce.domain.grid_export"))
    reload(import_module("custom_components.smart_rce.domain.ems"))
    reload(import_module("custom_components.smart_rce.domain"))
    reload(import_module("custom_components.smart_rce.adapter"))
    reload(import_module("custom_components.smart_rce.domain.pv_forecast"))
    reload(import_module("custom_components.smart_rce.weather_forecast_history"))
    reload(import_module("custom_components.smart_rce.weather_listener"))
    reload(import_module("custom_components.smart_rce.pv_forecast_coordinator"))
    reload(import_module("custom_components.smart_rce.coordinator"))
    reload(import_module("custom_components.smart_rce.sensor"))
    reload(import_module("custom_components.smart_rce.binary_sensor"))
    reload(import_module("custom_components.smart_rce"))


async def async_unload_entry(hass: HomeAssistant, entry: SmartRceConfigEntry) -> bool:
    """Unload a config entry."""
    live_reload()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
