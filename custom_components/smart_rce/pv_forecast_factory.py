"""Composition root: instancjonuje PvForecastService + adapters + wires HA listenery.

Mała "wiring" warstwa łącząca application layer (`PvForecastService` orchestrator)
z infrastructure (driven + driving adapters) dla HA. Wywoływana z
`__init__.py::async_setup_entry`.

Layer responsibility (DDD):
- domain/pv_forecast.py — PvForecast aggregate (state + zachowanie),
  pure algorytmy (adjust_pv_forecast_*, calculate_target_soc), value objects
- application/pv_forecast_service.py — PvForecastService orchestrator
  (read from adapters → call domain update → notify listeners). HASS-FREE.
- infrastructure/pv_forecast/ — driving/driven adapters (SolcastReader,
  WeatherConditionsBuilder, ConsumptionProfileLoader)
- pv_forecast_factory.py — composition root, wires hass + adapters + service
  + HA listenery + initial task

Service nie wie o `hass` — factory wraps sync→async gdzie trzeba
(`hass.async_create_task` dla daily profile refresh).
"""

from __future__ import annotations

from datetime import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)

from .application.pv_forecast_service import PvForecastService
from .domain.pv_forecast import PvForecast
from .infrastructure.pv_forecast.consumption_profile_loader import (
    ConsumptionProfileLoader,
)
from .infrastructure.pv_forecast.solcast_reader import SolcastReader
from .weather_forecast_history import WeatherForecastHistory
from .weather_listener import WeatherListenerCoordinator

_LOGGER = logging.getLogger(__name__)


async def create_pv_forecast_service(
    hass: HomeAssistant,
    entry: ConfigEntry,
    weather_listener: WeatherListenerCoordinator,
    weather_forecast_history: WeatherForecastHistory,
) -> PvForecastService:
    """Composition root — wire domain + adapters + service + HA listenery."""
    forecast = PvForecast()
    solcast = SolcastReader(hass)
    consumption_loader = ConsumptionProfileLoader(hass)

    service = PvForecastService(
        forecast=forecast,
        solcast=solcast,
        weather_listener=weather_listener,
        weather_history=weather_forecast_history,
        consumption_loader=consumption_loader,
    )

    # Weather forecast updates (Wetteronline integration).
    weather_listener.async_add_listener(service.on_weather_update)

    # Solcast entity state_changed (3 entity ids ukryte w SolcastReader).
    at6_id, live_id, tomorrow_id = solcast.entity_ids
    entry.async_on_unload(
        async_track_state_change_event(hass, [at6_id], service.on_solcast_at6_change)
    )
    entry.async_on_unload(
        async_track_state_change_event(hass, [live_id], service.on_solcast_live_change)
    )
    entry.async_on_unload(
        async_track_state_change_event(
            hass, [tomorrow_id], service.on_solcast_tomorrow_change
        )
    )

    # Daily prev-workday consumption profile refresh at 05:55 local — sync
    # callback wraps async refresh_profiles via hass.async_create_task,
    # żeby Service zostało hass-free.
    @callback
    def _on_daily_refresh(_now: datetime) -> None:
        hass.async_create_task(service.refresh_profiles())

    entry.async_on_unload(
        async_track_time_change(hass, _on_daily_refresh, hour=5, minute=55, second=0)
    )

    # Initial calculation + background profile fetch.
    service.recalculate_all()
    hass.async_create_task(service.refresh_profiles())

    return service
