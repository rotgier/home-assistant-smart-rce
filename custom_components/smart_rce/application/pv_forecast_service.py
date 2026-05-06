"""PvForecastService — application service orchestrating weather-adjusted PV estimates.

DDD application layer (analog Ems w application/ems.py): trzyma `PvForecast`
aggregate (state + behavior), orchestruje read-from-adapters → call domain
update method → notify listeners.

Consumes:
- driving adapter `infrastructure/pv_forecast_loader.py` (Solcast/weather sources)
- driven adapter `infrastructure/consumption_profile_loader.py` (HA recorder LTS)

State + algorytmy żyją w `domain/pv_forecast.PvForecast`. Service nie trzyma
żadnego stanu domenowego — tylko technical lifecycle (HA listener cancellers,
listener registry).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util

from ..domain.pv_forecast import PvForecast
from ..infrastructure.pv_forecast.consumption_profile_loader import (
    ConsumptionProfileLoader,
)
from ..infrastructure.pv_forecast.solcast_reader import SolcastReader
from ..infrastructure.pv_forecast.weather_conditions_builder import (
    WeatherConditionsBuilder,
)
from ..weather_forecast_history import WeatherForecastHistory
from ..weather_listener import WeatherListenerCoordinator

_LOGGER = logging.getLogger(__name__)


class PvForecastService:
    """Orchestrates Solcast/weather reads → PvForecast aggregate updates → listeners."""

    def __init__(
        self,
        hass: HomeAssistant,
        weather_coordinator: WeatherListenerCoordinator,
        weather_forecast_history: WeatherForecastHistory,
    ) -> None:
        self._hass = hass
        self._weather_coordinator = weather_coordinator
        self._solcast = SolcastReader(hass)
        self._weather_builder = WeatherConditionsBuilder(
            weather_coordinator, weather_forecast_history
        )
        self._consumption_loader = ConsumptionProfileLoader(hass)
        self._listeners: dict[CALLBACK_TYPE, CALLBACK_TYPE] = {}
        self._cancel_solcast_listeners: list[CALLBACK_TYPE] = []
        self._cancel_profile_refresh: CALLBACK_TYPE | None = None
        self.forecast: PvForecast = PvForecast()

    async def async_start(self) -> None:
        """Start listening for weather and Solcast changes."""
        self._weather_coordinator.async_add_listener(self._on_weather_update)

        at6_id, live_id, tomorrow_id = self._solcast.entity_ids
        cancel_at6 = async_track_state_change_event(
            self._hass, [at6_id], self._on_solcast_at6_change
        )
        cancel_live = async_track_state_change_event(
            self._hass, [live_id], self._on_solcast_live_change
        )
        cancel_tomorrow = async_track_state_change_event(
            self._hass, [tomorrow_id], self._on_solcast_tomorrow_change
        )
        self._cancel_solcast_listeners = [cancel_at6, cancel_live, cancel_tomorrow]

        # Daily prev-workday consumption profile refresh at 05:55 local.
        self._cancel_profile_refresh = async_track_time_change(
            self._hass, self._on_daily_profile_refresh, hour=5, minute=55, second=0
        )

        # Initial calculation
        self._recalculate_all()
        # Initial profile fetch (background; update_consumption_profiles will recalc).
        self._hass.async_create_task(self._refresh_profiles())

    def async_stop(self) -> None:
        """Stop listening."""
        for cancel in self._cancel_solcast_listeners:
            cancel()
        self._cancel_solcast_listeners = []
        if self._cancel_profile_refresh:
            self._cancel_profile_refresh()
            self._cancel_profile_refresh = None

    @callback
    def _on_weather_update(self) -> None:
        """Weather forecast changed — recalculate all forecasts."""
        _LOGGER.debug("Weather update received, recalculating PV forecasts")
        self._recalculate_all()

    def _recalculate_all(self) -> None:
        """Recalculate all forecasts (at_6, live, tomorrow) + notify."""
        self._recalculate_at6()
        self._recalculate_live()
        self._recalculate_tomorrow()
        self._notify_listeners()

    def _recalculate_at6(self) -> None:
        """Recalculate AT6 forecast.

        Before 6:01 — use live Solcast (has forecast fetched at 22:00).
        After 6:01 — use at_6 snapshot (fresh for today).
        """
        now = dt_util.now()
        if now.hour < 6 or (now.hour == 6 and now.minute < 2):
            solcast_periods = self._solcast.read_live()
            source = "live (pre-6:01)"
        else:
            solcast_periods = self._solcast.read_at_6()
            source = "at_6"

        if not solcast_periods:
            return

        weather = self._weather_builder.build(now.date())
        self.forecast.update_at_6(solcast_periods, weather, now)
        _LOGGER.debug(
            "Adjusted at_6 (source: %s): %.1f kWh (from %d periods, %d weather conditions)",
            source,
            self.forecast.adjusted_at_6.total_kwh,
            len(self.forecast.adjusted_at_6.forecast),
            len(weather),
        )

    def _recalculate_live(self) -> None:
        """Recalculate weather-adjusted forecast from live Solcast."""
        solcast_periods = self._solcast.read_live()
        if not solcast_periods:
            return
        now = dt_util.now()
        weather = self._weather_builder.build(now.date())
        self.forecast.update_live(solcast_periods, weather, now)
        _LOGGER.debug(
            "Adjusted live: %.1f kWh (from %d periods)",
            self.forecast.adjusted_live.total_kwh,
            len(self.forecast.adjusted_live.forecast),
        )

    def _recalculate_tomorrow(self) -> None:
        """Recalculate tomorrow forecast — both AT6 and LIVE variants."""
        solcast_periods = self._solcast.read_tomorrow()
        if not solcast_periods:
            return
        now = dt_util.now()
        tomorrow = (now + timedelta(days=1)).date()
        weather = self._weather_builder.build(tomorrow)
        self.forecast.update_tomorrow(solcast_periods, weather, now)
        _LOGGER.debug(
            "Adjusted tomorrow: AT6=%.1f kWh, LIVE=%.1f kWh (from %d periods, %d weather conditions)",
            self.forecast.adjusted_tomorrow.total_kwh,
            self.forecast.adjusted_tomorrow_live.total_kwh,
            len(self.forecast.adjusted_tomorrow.forecast),
            len(weather),
        )

    @callback
    def _on_solcast_at6_change(self, event: Event) -> None:
        """Solcast at_6 snapshot changed — recalculate at_6."""
        _LOGGER.debug("Solcast at_6 changed, recalculating")
        self._recalculate_at6()
        self._notify_listeners()

    @callback
    def _on_solcast_live_change(self, event: Event) -> None:
        """Solcast live changed — recalculate live."""
        _LOGGER.debug("Solcast live changed, recalculating")
        self._recalculate_live()
        self._notify_listeners()

    @callback
    def _on_solcast_tomorrow_change(self, event: Event) -> None:
        """Solcast tomorrow changed — recalculate tomorrow."""
        _LOGGER.debug("Solcast tomorrow changed, recalculating")
        self._recalculate_tomorrow()
        self._notify_listeners()

    @callback
    def _on_daily_profile_refresh(self, _now: datetime) -> None:
        """Scheduled at 05:55 local — refresh prev-workday consumption profiles."""
        self._hass.async_create_task(self._refresh_profiles())

    async def _refresh_profiles(self) -> None:
        """Fetch profiles + update aggregate + notify listeners."""
        now = dt_util.now()
        try:
            profiles = await self._consumption_loader.fetch(now.date())
        except Exception:  # noqa: BLE001 — defensive, don't crash integration
            _LOGGER.exception("Failed to fetch consumption profiles")
            return
        self.forecast.update_consumption_profiles(profiles, now)
        self._notify_listeners()

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        def remove_listener() -> None:
            self._listeners.pop(remove_listener)

        self._listeners[remove_listener] = update_callback
        return remove_listener

    def _notify_listeners(self) -> None:
        for update_callback in list(self._listeners.values()):
            update_callback()
