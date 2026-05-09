"""The Smart RCE component."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module, reload
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .application.ems import Ems
from .application.pv_forecast_service import PvForecastService
from .coordinator import SmartRceDataUpdateCoordinator
from .domain.weather_forecast_history import WeatherForecastHistory
from .ems_factory import create_ems
from .infrastructure.rce_api import RceApi
from .infrastructure.weather_listener import WeatherForecastListener
from .pv_forecast_factory import create_pv_forecast_service

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


@dataclass
class SmartRceData:
    """Smart Rce Data."""

    ems: Ems
    rce_coordinator: SmartRceDataUpdateCoordinator
    weather_listener: WeatherForecastListener
    pv_forecast: PvForecastService
    weather_forecast_history: WeatherForecastHistory


type SmartRceConfigEntry = ConfigEntry[SmartRceData]


async def async_setup_entry(hass: HomeAssistant, entry: SmartRceConfigEntry) -> bool:
    """Set up Smart RCE as config entry."""
    _LOGGER.debug("async_setup_entry")

    websession = async_get_clientsession(hass)
    rceApi = RceApi(websession)
    ems: Ems = await create_ems(hass, entry)
    rce_coordinator = SmartRceDataUpdateCoordinator(hass, rceApi, ems, entry)
    weather_listener = WeatherForecastListener(hass, entry)

    weather_forecast_history = WeatherForecastHistory()
    pv_forecast = await create_pv_forecast_service(
        hass, entry, weather_listener, weather_forecast_history
    )

    await rce_coordinator.async_config_entry_first_refresh()

    entry.runtime_data = SmartRceData(
        ems,
        rce_coordinator,
        weather_listener,
        pv_forecast,
        weather_forecast_history,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


def live_reload():
    reload(import_module("custom_components.smart_rce.const"))
    reload(import_module("custom_components.smart_rce.infrastructure.rce_api"))
    reload(import_module("custom_components.smart_rce.infrastructure"))
    reload(import_module("custom_components.smart_rce.domain.rce"))
    reload(import_module("custom_components.smart_rce.domain.input_state"))
    reload(import_module("custom_components.smart_rce.domain.battery"))
    reload(import_module("custom_components.smart_rce.domain.block_discharge"))
    # grid_export PRZED water_heater — water_heater importuje InterventionDirection
    # z grid_export. Reload water_heater pre-grid_export → top-level import
    # zwraca OLD class → cross-module `is`/`==` identity break (`is` fail bo
    # różny class object; `==` OK bo StrEnum value-based).
    # Order in package: intervention (Protocol/VOs base) → positive/negative
    # → manager → __init__ (re-exports).
    reload(import_module("custom_components.smart_rce.domain.grid_export.intervention"))
    reload(import_module("custom_components.smart_rce.domain.grid_export.positive"))
    reload(import_module("custom_components.smart_rce.domain.grid_export.negative"))
    reload(import_module("custom_components.smart_rce.domain.grid_export.manager"))
    reload(import_module("custom_components.smart_rce.domain.grid_export"))
    reload(import_module("custom_components.smart_rce.domain.water_heater"))
    reload(import_module("custom_components.smart_rce.domain.charge_slots"))
    reload(import_module("custom_components.smart_rce.domain.discharge_slots"))
    reload(import_module("custom_components.smart_rce.domain.ems_rce_prices"))
    reload(import_module("custom_components.smart_rce.domain.dod_policy"))
    reload(import_module("custom_components.smart_rce.application.ems"))
    reload(import_module("custom_components.smart_rce.application"))
    reload(import_module("custom_components.smart_rce.domain"))
    # infrastructure modules PRZED ems_factory — composition root importuje
    # wszystkie 3 driven adapters + state_mapper driving adapter.
    reload(import_module("custom_components.smart_rce.infrastructure.state_mapper"))
    reload(
        import_module("custom_components.smart_rce.infrastructure.battery_persistence")
    )
    reload(import_module("custom_components.smart_rce.infrastructure.battery_logger"))
    reload(
        import_module("custom_components.smart_rce.infrastructure.grid_export_actuator")
    )
    reload(import_module("custom_components.smart_rce.ems_factory"))
    reload(import_module("custom_components.smart_rce.domain.pv_forecast"))
    reload(
        import_module("custom_components.smart_rce.domain.pv_forecast_extrapolation")
    )
    reload(import_module("custom_components.smart_rce.domain.weather_forecast_history"))
    reload(import_module("custom_components.smart_rce.infrastructure.weather_listener"))
    reload(
        import_module("custom_components.smart_rce.infrastructure.weather_diff_writer")
    )
    reload(
        import_module(
            "custom_components.smart_rce.infrastructure.pv_forecast.solcast_reader"
        )
    )
    reload(
        import_module(
            "custom_components.smart_rce.infrastructure.pv_forecast.consumption_profile_loader"
        )
    )
    reload(
        import_module(
            "custom_components.smart_rce.infrastructure.pv_forecast.live_rate_reader"
        )
    )
    reload(
        import_module(
            "custom_components.smart_rce.infrastructure.pv_forecast.realized_pv_loader"
        )
    )
    reload(import_module("custom_components.smart_rce.infrastructure.pv_forecast"))
    reload(import_module("custom_components.smart_rce.application.pv_forecast_service"))
    reload(import_module("custom_components.smart_rce.pv_forecast_factory"))
    reload(import_module("custom_components.smart_rce.coordinator"))
    reload(import_module("custom_components.smart_rce.sensor._state_writer_mixin"))
    reload(import_module("custom_components.smart_rce.sensor.rce_sensor"))
    reload(import_module("custom_components.smart_rce.sensor.pv_forecast_sensor"))
    reload(import_module("custom_components.smart_rce.sensor.weather_history_sensor"))
    reload(import_module("custom_components.smart_rce.sensor.ems_sensor"))
    reload(import_module("custom_components.smart_rce.sensor"))
    reload(import_module("custom_components.smart_rce.binary_sensor"))
    reload(import_module("custom_components.smart_rce"))


async def async_unload_entry(hass: HomeAssistant, entry: SmartRceConfigEntry) -> bool:
    """Unload a config entry."""
    live_reload()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
