"""Composition root: instantiates PvForecastService + adapters + wires HA listeners.

Small "wiring" layer connecting the application layer (`PvForecastService`
orchestrator) with infrastructure (driven + driving adapters) for HA. Called
from `__init__.py::async_setup_entry`.

Layer responsibility (DDD):
- domain/pv_forecast.py — PvForecast aggregate (state + behavior), per-class
  pure algorithms (PvForecast._adjust_pv_forecast_*, PvForecast._calculate_target_soc),
  value objects, plus standalone domain utilities (merge_weather_conditions,
  walk_back_workdays) shared across application + infrastructure
- application/pv_forecast_service.py — PvForecastService orchestrator
  (read from adapters → call domain update → notify listeners). HASS-FREE.
- infrastructure/pv_forecast/ — driving/driven adapters (SolcastReader,
  WeatherConditionsBuilder, ConsumptionProfileLoader)
- pv_forecast_factory.py — composition root, wires hass + adapters + service
  + HA listeners + initial task

Service does not know about `hass` — factory wraps sync→async where needed
(`hass.async_create_task` for daily profile refresh).
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
from homeassistant.util import dt as dt_util

from .application.pv_forecast_service import PvForecastService
from .domain.pv_forecast import PvForecast
from .domain.weather_forecast_history import WeatherForecastHistory
from .infrastructure.pv_forecast.consumption_profile_loader import (
    ConsumptionProfileLoader,
)
from .infrastructure.pv_forecast.live_rate_reader import LiveRateReader
from .infrastructure.pv_forecast.solcast_reader import SolcastReader
from .infrastructure.weather_diff_writer import WeatherDiffWriter
from .infrastructure.weather_listener import WeatherForecastListener

_LOGGER = logging.getLogger(__name__)


async def create_pv_forecast_service(
    hass: HomeAssistant,
    entry: ConfigEntry,
    weather_listener: WeatherForecastListener,
    weather_forecast_history: WeatherForecastHistory,
) -> PvForecastService:
    """Composition root — wire domain + adapters + service + HA listenery."""
    forecast = PvForecast()
    solcast = SolcastReader(hass)
    consumption_loader = ConsumptionProfileLoader(hass)
    live_rates = LiveRateReader(hass)

    service = PvForecastService(
        forecast=forecast,
        solcast=solcast,
        weather_listener=weather_listener,
        weather_history=weather_forecast_history,
        consumption_loader=consumption_loader,
        live_rates=live_rates,
    )

    # Weather history write side — registered FIRST listener przed sensors,
    # żeby aggregate był zaktualizowany zanim sensory czytają state.
    # Side effect: zapisuje formatted diff do pliku przy każdym istotnym
    # change (initial fetch + actual condition diffs po hour rollover).
    diff_writer = WeatherDiffWriter(hass)

    @callback
    def _update_weather_history() -> None:
        now = dt_util.now()
        result = weather_forecast_history.update_from_forecast(
            weather_listener.forecast_hourly, now.date(), now
        )
        if result:
            diff_text, is_initial = result
            hass.async_create_task(diff_writer.write(diff_text, is_initial, now))

    entry.async_on_unload(weather_listener.async_add_listener(_update_weather_history))

    # Weather forecast updates → PV forecast service recalculation.
    entry.async_on_unload(
        weather_listener.async_add_listener(service.on_weather_update)
    )

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

    # Per-minute tick — refresh extrapolated variants so sensors reflect
    # shrinking remaining_fraction within the in-progress 30-min bucket.
    @callback
    def _on_minute_tick(_now: datetime) -> None:
        service.on_minute_tick()

    entry.async_on_unload(async_track_time_change(hass, _on_minute_tick, second=0))

    # Initial calculation + background profile fetch.
    service.recalculate_all()
    hass.async_create_task(service.refresh_profiles())

    return service
