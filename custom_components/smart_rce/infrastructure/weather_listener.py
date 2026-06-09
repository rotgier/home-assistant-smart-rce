"""WeatherForecastListener — driving adapter dla HA weather entity.

Wraps HA weather entity (`weather.wetteronline`):
- Subscribes to forecast updates (push-style via async_subscribe_forecast)
- Tracks entity availability (re-register on entity reappear)
- Exposes `forecast_conditions` property — parsed domain types
  (HA dict shape → list[WeatherConditionAtHour])
- Listener fan-out (technical event dispatch, używane przez EnergyBalanceService
  + WeatherForecastHistorySensor)

Hexagonal pattern: **driving adapter (inbound)** — adapts HA push-style API
to domain types (forecast_conditions zwraca list[WeatherConditionAtHour]).
Konsumenci subscribed listenerów to application/presentation: factory
(write side hook), EnergyBalanceService (recalculation trigger), sensor
(state refresh).
"""

from collections.abc import Callable
from datetime import datetime
import logging
from typing import Final

from homeassistant.components.weather import DOMAIN as WEATHER, WeatherEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    CoreState,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.event import Event, async_track_state_change_event
from homeassistant.util.json import JsonValueType

from ..domain.pv_forecast import WeatherConditionAtHour

WEATHER_ENTITY: Final[str] = "weather.wetteronline"
UNAVAILABLE_STATES: Final[tuple[str | None, ...]] = (
    STATE_UNKNOWN,
    STATE_UNAVAILABLE,
    "",
    None,
)


_LOGGER = logging.getLogger(__name__)


class WeatherForecastListener:
    """Weather listener coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        self.hass: HomeAssistant = hass
        self.forecast_hourly: list[JsonValueType] | None = None
        self._hass_started: bool = False
        self._shutdown_requested: bool = False
        self._listeners: dict[CALLBACK_TYPE, CALLBACK_TYPE] = {}
        self._unsubscribe_callback: CALLBACK_TYPE | None = None

        _LOGGER.debug("WeatherForecastListener init")
        entry.async_on_unload(self._shutdown)

        @callback
        def hass_started(_: Event | None = None) -> None:
            _LOGGER.debug("hass_started")
            self._hass_started = True
            self._register_for_weather_updates()

        if hass.state == CoreState.running:
            _LOGGER.debug("hass is already running")
            hass_started()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, hass_started)

        @callback
        def weather_state_changed(event: Event[EventStateChangedData]) -> None:
            old_obj = event.data["old_state"]
            new_obj = event.data["new_state"]
            old_value: str | None = old_obj.state if old_obj else None
            new_value: str | None = new_obj.state if new_obj else None
            if (old_value in UNAVAILABLE_STATES) != (new_value in UNAVAILABLE_STATES):
                msg = "weather entity %s availability changed from: '%s' to: '%s'"
                _LOGGER.debug(msg, WEATHER_ENTITY, old_value, new_value)

            if (
                old_value in UNAVAILABLE_STATES
                and new_value not in UNAVAILABLE_STATES
                and self._hass_started
            ):
                _LOGGER.debug("weather entity appeared -> register_for_weather_updates")
                self._register_for_weather_updates()

        entry.async_on_unload(
            async_track_state_change_event(
                self.hass,
                [WEATHER_ENTITY],
                weather_state_changed,
            )
        )

    @property
    def forecast_conditions(self) -> list[WeatherConditionAtHour]:
        """Parsed forecast hourly — domain types z HA weather entity.

        Driving adapter responsibility: parsuje raw HA-shape (`list[dict]`)
        do domain `WeatherConditionAtHour`. Application service consumes
        bezpośrednio bez znajomości HA dict shape.
        """
        return self._parse_forecast_hourly(self.forecast_hourly)

    @staticmethod
    def _parse_forecast_hourly(
        forecast_hourly: list[JsonValueType] | None,
    ) -> list[WeatherConditionAtHour]:
        """Parse HA weather forecast attribute → domain WeatherConditionAtHour."""
        if not forecast_hourly:
            return []
        result: list[WeatherConditionAtHour] = []
        for item in forecast_hourly:
            if not isinstance(item, dict):
                continue
            dt_value = item.get("datetime")
            if not isinstance(dt_value, str):
                continue
            condition = item.get("condition_custom", "cloudy")
            if not isinstance(condition, str):
                condition = "cloudy"
            result.append(
                WeatherConditionAtHour(
                    hour=datetime.fromisoformat(dt_value).hour,
                    condition_custom=condition,
                    forecast_date=datetime.fromisoformat(dt_value).date(),
                )
            )
        return result

    @callback
    def _register_for_weather_updates(self) -> None:
        _LOGGER.debug("_register_for_weather_updates")
        component: EntityComponent[WeatherEntity] = self.hass.data[WEATHER]
        entity: WeatherEntity | None = component.get_entity(WEATHER_ENTITY)
        if entity:
            _LOGGER.debug("weather entity is available")
            self._unregister_weather_updates()

            @callback
            def forecast_listener(forecast: list[JsonValueType] | None) -> None:
                if not self._shutdown_requested:
                    if self.forecast_hourly != forecast:
                        _LOGGER.debug("forecast_listener: forecast updated")
                        self.forecast_hourly = forecast
                        self._async_update_listeners()
                    else:
                        _LOGGER.debug("forecast_listener: forecast stale")

            weather_updates_unsubscribe = entity.async_subscribe_forecast(
                "hourly", forecast_listener
            )

            @callback
            def unsubscribe_callback() -> None:
                if self._unsubscribe_callback == unsubscribe_callback:
                    _LOGGER.debug("unsubscribe_callback with valid callback")
                    weather_updates_unsubscribe()
                    self._unsubscribe_callback = None
                else:
                    _LOGGER.debug("unsubscribe_callback with INVALID callback")

            self._unsubscribe_callback = unsubscribe_callback
            entity.async_on_remove(unsubscribe_callback)
            _LOGGER.debug("Trigger weather_entity.async_update_listeners")
            self.hass.loop.create_task(entity.async_update_listeners(["hourly"]))
        else:
            _LOGGER.debug("weather entity is NOT available")

    @callback
    def _unregister_weather_updates(self) -> None:
        _LOGGER.debug("_unregister_weather_updates cb: %s", self._unsubscribe_callback)
        if self._unsubscribe_callback is not None:
            self._unsubscribe_callback()

    @callback
    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        """Listen for data updates."""
        _LOGGER.debug("async_add_listener")
        if self._shutdown_requested:
            # No-op unsubscribe — listener never registered.
            return lambda: None

        @callback
        def remove_listener() -> None:
            """Remove update listener."""
            _LOGGER.debug("remove_listener")
            self._listeners.pop(remove_listener)

        self._listeners[remove_listener] = update_callback

        return remove_listener

    @callback
    def _async_update_listeners(self) -> None:
        """Update all registered listeners."""
        _LOGGER.debug("_async_update_listeners")
        for update_callback in list(self._listeners.values()):
            update_callback()

    @callback
    def _shutdown(self) -> None:
        """Unregister from weather updates and ignore any incoming updates."""
        _LOGGER.debug("_shutdown")
        self._shutdown_requested = True
        self._unregister_weather_updates()
