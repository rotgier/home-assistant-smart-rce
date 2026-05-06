"""PvForecastService — application service orchestrating weather-adjusted PV estimates.

DDD application layer (analog Ems w application/ems.py): read from driving
adapters (Solcast, weather) → call domain `PvForecast.update_*` → notify
listeners. State + algorytmy żyją w domain — Service czyste orchestration.

HASS-FREE: dependencies injected przez `pv_forecast_factory.py` (composition
root). Service nie wie o HomeAssistant — sync→async wrapping (`hass.async_create_task`
dla daily refresh) robi factory.

Public callbacks (`on_*`) są wired w factory:
- weather_listener.async_add_listener(service.on_weather_update)
- async_track_state_change_event(SOLCAST_*, service.on_solcast_*_change)
- async_track_time_change(05:55, factory wrapper → service.refresh_profiles)
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

from homeassistant.core import CALLBACK_TYPE, Event, callback
from homeassistant.util import dt as dt_util

from ..domain.pv_forecast import (
    PvForecast,
    WeatherConditionAtHour,
    merge_weather_conditions,
)
from ..domain.weather_forecast_history import WeatherForecastHistory
from ..infrastructure.pv_forecast.consumption_profile_loader import (
    ConsumptionProfileLoader,
)
from ..infrastructure.pv_forecast.solcast_reader import SolcastReader
from ..infrastructure.weather_listener import WeatherForecastListener


class PvForecastService:
    """Orchestrates Solcast/weather reads → PvForecast aggregate updates → listeners."""

    def __init__(
        self,
        forecast: PvForecast,
        solcast: SolcastReader,
        weather_listener: WeatherForecastListener,
        weather_history: WeatherForecastHistory,
        consumption_loader: ConsumptionProfileLoader,
    ) -> None:
        self.forecast = forecast
        self._solcast = solcast
        self._weather_listener = weather_listener
        self._weather_history = weather_history
        self._consumption_loader = consumption_loader
        self._listeners: dict[CALLBACK_TYPE, CALLBACK_TYPE] = {}

    def recalculate_all(self) -> None:
        """Recalculate all forecasts (at_6, live, tomorrow) + notify listeners."""
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
        else:
            solcast_periods = self._solcast.read_at_6()
        if not solcast_periods:
            return
        weather = self._build_weather(now.date())
        self.forecast.update_at_6(solcast_periods, weather, now)

    def _recalculate_live(self) -> None:
        """Recalculate live weather-adjusted forecast."""
        solcast_periods = self._solcast.read_live()
        if not solcast_periods:
            return
        now = dt_util.now()
        weather = self._build_weather(now.date())
        self.forecast.update_live(solcast_periods, weather, now)

    def _recalculate_tomorrow(self) -> None:
        """Recalculate tomorrow forecast — both AT6 and LIVE variants."""
        solcast_periods = self._solcast.read_tomorrow()
        if not solcast_periods:
            return
        now = dt_util.now()
        tomorrow = (now + timedelta(days=1)).date()
        weather = self._build_weather(tomorrow)
        self.forecast.update_tomorrow(solcast_periods, weather, now)

    def _build_weather(self, day: date) -> list[WeatherConditionAtHour]:
        """Combine weather history (past hours) + live forecast (future hours).

        Multi-caller helper — używane przez 3× `_recalculate_*`. Pure orchestration:
        read 2 sources + delegate merge do domain `merge_weather_conditions`.
        """
        history = self._weather_history.get_conditions_for_date(day)
        forecast = self._weather_listener.forecast_conditions
        return merge_weather_conditions(history, forecast)

    @callback
    def on_weather_update(self) -> None:
        """Weather forecast changed — recalculate all (history+forecast affects all)."""
        self.recalculate_all()

    @callback
    def on_solcast_at6_change(self, event: Event) -> None:
        """Solcast at_6 snapshot changed — recalculate at_6."""
        self._recalculate_at6()
        self._notify_listeners()

    @callback
    def on_solcast_live_change(self, event: Event) -> None:
        """Solcast live changed — recalculate live."""
        self._recalculate_live()
        self._notify_listeners()

    @callback
    def on_solcast_tomorrow_change(self, event: Event) -> None:
        """Solcast tomorrow changed — recalculate tomorrow."""
        self._recalculate_tomorrow()
        self._notify_listeners()

    async def refresh_profiles(self) -> None:
        """Fetch prev-workday consumption profiles + update aggregate + notify."""
        now = dt_util.now()
        try:
            profiles = await self._consumption_loader.fetch(now.date())
        except Exception:  # noqa: BLE001 — defensive, don't crash integration
            return
        self.forecast.update_consumption_profiles(profiles, now)
        self._notify_listeners()

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        """Register listener for forecast/SoC change events. Returns unsubscribe fn."""

        def remove_listener() -> None:
            self._listeners.pop(remove_listener)

        self._listeners[remove_listener] = update_callback
        return remove_listener

    def _notify_listeners(self) -> None:
        for update_callback in list(self._listeners.values()):
            update_callback()
