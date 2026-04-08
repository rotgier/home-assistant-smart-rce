"""PV Forecast Coordinator — orchestrates weather-adjusted PV estimates."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging
from typing import Any, Final

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .domain.pv_forecast import (
    AdjustedPvForecast,
    SolcastPeriod,
    WeatherConditionAtHour,
    adjust_pv_forecast_at6,
    adjust_pv_forecast_live,
    calculate_target_soc,
)
from .weather_listener import WeatherListenerCoordinator

SOLCAST_AT_6_ENTITY: Final = "sensor.solcast_forecast_at_6"
SOLCAST_LIVE_ENTITY: Final = "sensor.solcast_pv_forecast_prognoza_na_dzisiaj"

_LOGGER = logging.getLogger(__name__)


def _parse_solcast_forecast(
    forecast_attr: list[dict[str, Any]],
) -> list[SolcastPeriod]:
    """Parse Solcast forecast attribute into domain objects."""
    return [
        SolcastPeriod(
            period_start=str(item["period_start"]),
            pv_estimate=item["pv_estimate"],
            pv_estimate10=item["pv_estimate10"],
            pv_estimate90=item["pv_estimate90"],
        )
        for item in forecast_attr
    ]


def _parse_weather_conditions(
    forecast_hourly: list[dict[str, Any]] | None,
) -> list[WeatherConditionAtHour]:
    """Parse WeatherListenerCoordinator.forecast_hourly into domain objects.

    Returns conditions with both hour and date, to allow matching
    against the correct day in Solcast forecast.
    """
    if not forecast_hourly:
        return []
    return [
        WeatherConditionAtHour(
            hour=datetime.fromisoformat(item["datetime"]).hour,
            date=datetime.fromisoformat(item["datetime"]).date(),
            condition_custom=item.get("condition_custom", "cloudy"),
        )
        for item in forecast_hourly
        if "datetime" in item
    ]


def _is_workday(now: datetime) -> bool:
    """Check if today is a workday (Mon-Fri). Does not check holidays."""
    return now.weekday() < 5


class PvForecastCoordinator:
    """Coordinates weather-adjusted PV forecast calculation."""

    def __init__(
        self,
        hass: HomeAssistant,
        weather_coordinator: WeatherListenerCoordinator,
    ) -> None:
        self._hass = hass
        self._weather_coordinator = weather_coordinator
        self._listeners: dict[CALLBACK_TYPE, CALLBACK_TYPE] = {}
        self._cancel_solcast_listeners: list[CALLBACK_TYPE] = []

        self.adjusted_at_6: AdjustedPvForecast | None = None
        self.adjusted_live: AdjustedPvForecast | None = None
        self.target_soc: int | None = None

    async def async_start(self) -> None:
        """Start listening for weather and Solcast changes."""
        # Listen to weather changes via existing WeatherListenerCoordinator
        self._weather_coordinator.async_add_listener(self._on_weather_update)

        # Listen to Solcast entity state changes
        cancel_at6 = async_track_state_change_event(
            self._hass, [SOLCAST_AT_6_ENTITY], self._on_solcast_at6_change
        )
        cancel_live = async_track_state_change_event(
            self._hass, [SOLCAST_LIVE_ENTITY], self._on_solcast_live_change
        )
        self._cancel_solcast_listeners = [cancel_at6, cancel_live]

        # Initial calculation
        self._recalculate_all()

    def async_stop(self) -> None:
        """Stop listening."""
        for cancel in self._cancel_solcast_listeners:
            cancel()
        self._cancel_solcast_listeners = []

    @callback
    def _on_weather_update(self) -> None:
        """Weather forecast changed — recalculate both."""
        _LOGGER.debug("Weather update received, recalculating PV forecasts")
        self._recalculate_all()

    @callback
    def _on_solcast_at6_change(self, event: Event) -> None:
        """Solcast at_6 snapshot changed — recalculate at_6."""
        _LOGGER.debug("Solcast at_6 changed, recalculating")
        self._recalculate_at6()
        self._recalculate_target_soc()
        self._notify_listeners()

    @callback
    def _on_solcast_live_change(self, event: Event) -> None:
        """Solcast live changed — recalculate live."""
        _LOGGER.debug("Solcast live changed, recalculating")
        self._recalculate_live()
        self._notify_listeners()

    def _recalculate_all(self) -> None:
        """Recalculate both forecasts and target SOC."""
        self._recalculate_at6()
        self._recalculate_live()
        self._recalculate_target_soc()
        self._notify_listeners()

    def _recalculate_at6(self) -> None:
        """Recalculate weather-adjusted forecast.

        Before 6:01 — use live Solcast (has forecast fetched at 22:00).
        After 6:01 — use at_6 snapshot (fresh for today).
        """
        from homeassistant.util.dt import now as now_local

        now = now_local()
        if now.hour < 6 or (now.hour == 6 and now.minute < 2):
            entity_id = SOLCAST_LIVE_ENTITY
            attr_name = "detailedForecast"
            source = "live (pre-6:01)"
        else:
            entity_id = SOLCAST_AT_6_ENTITY
            attr_name = "forecast"
            source = "at_6"

        solcast_periods = self._read_solcast_entity(entity_id, attr_name)
        if not solcast_periods:
            return

        weather = _parse_weather_conditions(self._weather_coordinator.forecast_hourly)
        self.adjusted_at_6 = adjust_pv_forecast_at6(solcast_periods, weather)
        _LOGGER.debug(
            "Adjusted at_6 (source: %s): %.1f kWh (from %d periods)",
            source,
            self.adjusted_at_6.total_kwh,
            len(self.adjusted_at_6.forecast),
        )

    def _recalculate_live(self) -> None:
        """Recalculate weather-adjusted forecast from live Solcast."""
        solcast_periods = self._read_solcast_entity(
            SOLCAST_LIVE_ENTITY, "detailedForecast"
        )
        if not solcast_periods:
            return

        weather = _parse_weather_conditions(self._weather_coordinator.forecast_hourly)
        from homeassistant.util.dt import now as now_local

        self.adjusted_live = adjust_pv_forecast_live(
            solcast_periods, weather, now_local()
        )
        _LOGGER.debug(
            "Adjusted live: %.1f kWh (from %d periods)",
            self.adjusted_live.total_kwh,
            len(self.adjusted_live.forecast),
        )

    def _recalculate_target_soc(self) -> None:
        """Calculate target battery SOC from adjusted at_6 forecast."""
        if not self.adjusted_at_6:
            return

        from homeassistant.util.dt import now as now_local

        now = now_local()
        self.target_soc = calculate_target_soc(
            self.adjusted_at_6, is_workday=_is_workday(now)
        )
        _LOGGER.debug("Target SOC: %d%%", self.target_soc)

    def _read_solcast_entity(
        self, entity_id: str, attr_name: str
    ) -> list[SolcastPeriod] | None:
        """Read Solcast forecast from HA state machine."""
        state = self._hass.states.get(entity_id)
        if not state:
            _LOGGER.debug("Entity %s not found", entity_id)
            return None

        forecast_attr = state.attributes.get(attr_name)
        if not forecast_attr:
            _LOGGER.debug("Entity %s has no attribute %s", entity_id, attr_name)
            return None

        return _parse_solcast_forecast(forecast_attr)

    # --- Listener pattern (same as Ems) ---

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        def remove_listener() -> None:
            self._listeners.pop(remove_listener)

        self._listeners[remove_listener] = update_callback
        return remove_listener

    def _notify_listeners(self) -> None:
        for update_callback in list(self._listeners.values()):
            update_callback()
