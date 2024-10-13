"""The Smart RCE component."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import importlib
import logging

# import custom_components.smart_rce
# import custom_components.smart_rce.domain
# import custom_components.smart_rce.domain.ems
# import custom_components.smart_rce.sensor
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.loader import DATA_CUSTOM_COMPONENTS, async_get_custom_components
from homeassistant.util.dt import now as now_local

from .coordinator import SmartRceDataUpdateCoordinator
from .domain.ems import Ems
from .infrastructure.rce_api import RceApi
from .weather_listener import WeatherListenerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


@dataclass
class SmartRceData:
    """Smart Rce Data."""

    ems: Ems
    rce_coordinator: SmartRceDataUpdateCoordinator
    weather_coordinator: WeatherListenerCoordinator


type SmartRceConfigEntry = ConfigEntry[SmartRceData]


async def async_setup_entry(hass: HomeAssistant, entry: SmartRceConfigEntry) -> bool:
    """Set up Smart RCE as config entry."""
    _LOGGER.debug("async_setup_entry")

    websession = async_get_clientsession(hass)
    rceApi = RceApi(websession)
    ems: Ems = create_ems(hass, entry)
    rce_coordinator = SmartRceDataUpdateCoordinator(hass, rceApi, ems, entry)
    weather_coordinator = WeatherListenerCoordinator(hass, entry)

    await rce_coordinator.async_config_entry_first_refresh()

    entry.runtime_data = SmartRceData(ems, rce_coordinator, weather_coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


def create_ems(hass: HomeAssistant, entry: SmartRceConfigEntry) -> Ems:
    ems: Ems = Ems()

    @callback
    def update_current_price(now: datetime) -> None:
        ems.update_now(now)

    update_current_price(now_local())
    entry.async_on_unload(
        async_track_time_change(hass, update_current_price, minute=0, second=0)
    )
    return ems


async def async_unload_entry(hass: HomeAssistant, entry: SmartRceConfigEntry) -> bool:
    """Unload a config entry."""
    mod = importlib.import_module("custom_components.smart_rce")
    importlib.reload(mod)
    mod = importlib.import_module("custom_components.smart_rce.sensor")
    importlib.reload(mod)
    mod = importlib.import_module("custom_components.smart_rce.domain")
    importlib.reload(mod)
    mod = importlib.import_module("custom_components.smart_rce.domain.ems")
    importlib.reload(mod)

    # importlib.reload(custom_components.smart_rce.sensor)
    # importlib.reload(custom_components.smart_rce.domain)
    # importlib.reload(custom_components.smart_rce.domain.ems)
    # exec("import custom_components.smart_rce")

    smart_rce_integration = hass.data[DATA_CUSTOM_COMPONENTS]["smart_rce"]
    smart_rce_integration._cache.pop("smart_rce")  # noqa: SLF001
    smart_rce_integration._cache.pop("smart_rce.config_flow")  # noqa: SLF001
    smart_rce_integration._cache.pop("smart_rce.sensor")  # noqa: SLF001
    smart_rce_integration._cache.pop("sensor")  # noqa: SLF001
    smart_rce_integration = hass.data[DATA_CUSTOM_COMPONENTS]["smart_rce"]

    result = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    hass.data.pop(DATA_CUSTOM_COMPONENTS)
    await async_get_custom_components(hass)
    smart_rce_integration = hass.data[DATA_CUSTOM_COMPONENTS]["smart_rce"]
    # smart_rce_integration._cache.pop("smart_rce")
    # smart_rce_integration._cache.pop("smart_rce.config_flow")
    # smart_rce_integration._cache.pop("smart_rce.sensor")
    smart_rce_integration._cache.pop("sensor")  # noqa: SLF001
    smart_rce_integration = hass.data[DATA_CUSTOM_COMPONENTS]["smart_rce"]
    return result
