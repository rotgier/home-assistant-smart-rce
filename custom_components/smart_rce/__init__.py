"""The Smart RCE component."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .coordinator import SmartRceDataUpdateCoordinator, WeatherListenerCoordinator
from .rce_api import RceApi

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


@dataclass
class SmartRceData:
    """Smart Rce Data."""

    rce_coordinator: SmartRceDataUpdateCoordinator
    weather_coordinator: WeatherListenerCoordinator


type SmartRceConfigEntry = ConfigEntry[SmartRceData]


async def async_setup_entry(hass: HomeAssistant, entry: SmartRceConfigEntry) -> bool:
    """Set up Smart RCE as config entry."""
    _LOGGER.debug("async_setup_entry")

    websession = async_get_clientsession(hass)
    rceApi = RceApi(websession)
    rce_coordinator = SmartRceDataUpdateCoordinator(hass, rceApi, entry)
    weather_coordinator = WeatherListenerCoordinator(hass, entry)

    await rce_coordinator.async_config_entry_first_refresh()

    entry.runtime_data = SmartRceData(rce_coordinator, weather_coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: SmartRceConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
