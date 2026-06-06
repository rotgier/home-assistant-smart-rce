"""Composition root: instantiates PvForecastService + adapters + wires HA listeners.

Small "wiring" layer connecting the application layer (`PvForecastService`
orchestrator) with infrastructure (driven + driving adapters) for HA. Called
from `__init__.py::async_setup_entry`.

Layer responsibility (DDD):
- domain/pv_forecast.py — TargetSocCatalog aggregate (state + behavior), per-class
  pure algorithms (TargetSocCatalog._adjust_pv_forecast_*), value objects, plus
  standalone domain utilities (merge_weather_conditions, walk_back_workdays)
  shared across application + infrastructure
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
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util

from .application.ems import Ems
from .application.pv_forecast_service import PvForecastService
from .coordinator import SmartRceDataUpdateCoordinator
from .domain.pv_forecasts import PvForecasts
from .domain.target_soc_catalog import TargetSocCatalog
from .domain.weather_forecast_history import WeatherForecastHistory
from .infrastructure.pv_forecast.consumption_profile_loader import (
    ConsumptionProfileLoader,
)
from .infrastructure.pv_forecast.live_rate_reader import LiveRateReader
from .infrastructure.pv_forecast.realized_pv_loader import RealizedPvLoader
from .infrastructure.pv_forecast.solcast_reader import SolcastReader
from .infrastructure.weather_diff_writer import WeatherDiffWriter
from .infrastructure.weather_listener import WeatherForecastListener
from .infrastructure.workday_calendar_reader import WorkdayCalendarReader

_LOGGER = logging.getLogger(__name__)


async def create_pv_forecast_service(
    hass: HomeAssistant,
    entry: ConfigEntry,
    weather_listener: WeatherForecastListener,
    weather_forecast_history: WeatherForecastHistory,
    ems: Ems,
    rce_coordinator: SmartRceDataUpdateCoordinator,
) -> PvForecastService:
    """Composition root — wire domain + adapters + service + HA listenery."""
    updater = PvForecasts()
    target_socs = TargetSocCatalog()
    solcast = SolcastReader(hass)
    workday_reader = WorkdayCalendarReader(hass)
    consumption_loader = ConsumptionProfileLoader(hass, workday_reader)
    live_rates = LiveRateReader(hass)
    realized_pv_loader = RealizedPvLoader(hass)

    service = PvForecastService(
        hass=hass,
        updater=updater,
        target_socs=target_socs,
        solcast=solcast,
        weather_listener=weather_listener,
        weather_history=weather_forecast_history,
        consumption_loader=consumption_loader,
        live_rates=live_rates,
        realized_pv_loader=realized_pv_loader,
        charge_slots=ems.charge_slots,
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

    # Tomorrow pre-charge gate lives in the domain (`ems.charge_slots.tomorrow`).
    # Subscribe to coordinator so a fresh RCE-prices fetch (which recomputes
    # charge_slots) triggers a target_soc rebuild. Also catches the initial
    # setup race where charge_slots is still empty during the first
    # `recalculate_all` (first_refresh runs after pv_forecast factory).
    entry.async_on_unload(
        rce_coordinator.async_add_listener(service.on_charge_slots_change)
    )

    # Daily prev-workday consumption profile refresh at 05:55 local — sync
    # callback wraps async refresh via hass.async_create_task, żeby Service
    # zostało hass-free. Full refresh — day rollover, today-anchored
    # prev_1 (= yesterday workday) changes.
    @callback
    def _on_daily_refresh(_now: datetime) -> None:
        hass.async_create_task(service.refresh_profiles_full())

    entry.async_on_unload(
        async_track_time_change(hass, _on_daily_refresh, hour=5, minute=55, second=0)
    )

    # Per-minute tick — refresh extrapolated variants. Cached realized-PV
    # history is refreshed on bucket boundaries (every 30 min at :00/:30, +30s
    # offset to let the utility meter settle past reset).
    @callback
    def _on_minute_tick(_now: datetime) -> None:
        service.on_minute_tick()

    entry.async_on_unload(async_track_time_change(hass, _on_minute_tick, second=0))

    @callback
    def _on_bucket_boundary(now: datetime) -> None:
        hass.async_create_task(service.refresh_realized_pv())
        # Tomorrow-anchored prev_1 = today; its data grows as utility
        # meter cycles close during the PV window. Outside 07:30..13:30
        # nothing changes — skip the recorder hit.
        if (now.hour, now.minute) >= (7, 30) and (now.hour, now.minute) <= (13, 30):
            hass.async_create_task(service.refresh_profiles_tomorrow_only())

    entry.async_on_unload(
        async_track_time_change(hass, _on_bucket_boundary, minute=[0, 30], second=30)
    )

    # Initial sync recalc — uses the in-memory TargetSocCatalog state restored from
    # last shutdown (or the empty default on cold start). Safe to run before
    # async fetches return.
    service.recalculate_all()

    # Initial async fetches deferred to `EVENT_HOMEASSISTANT_STARTED`.
    # At `async_setup_entry` time the recorder + calendar.workday_calendar
    # integrations may not be fully loaded yet — `consumption_loader` would
    # then see an empty workday set and all 8 prev_X slots would be `None`,
    # leaving sensors stuck at `unknown` until the next scheduled trigger.
    # Waiting for STARTED guarantees both subsystems are ready.
    @callback
    def _on_ha_started(_event: Event) -> None:
        hass.async_create_task(service.refresh_profiles_full())
        hass.async_create_task(service.refresh_realized_pv())

    if hass.is_running:
        _on_ha_started(None)  # type: ignore[arg-type]
    else:
        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_ha_started)
        )

    return service
