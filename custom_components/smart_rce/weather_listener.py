"""Weather Listener Coordinator."""

from collections.abc import Callable
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

WEATHER_ENTITY: Final[str] = "weather.wetteronline"
UNAVAILABLE_STATES: Final[tuple[str | None]] = (
    STATE_UNKNOWN,
    STATE_UNAVAILABLE,
    "",
    None,
)


_LOGGER = logging.getLogger(__name__)


class WeatherListenerCoordinator:
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
        self._unsubscribe_callback: CALLBACK_TYPE = None

        _LOGGER.debug("WeatherListenerCoordinator init")
        entry.async_on_unload(self._shutdown)

        @callback
        def hass_started(_=Event) -> None:
            _LOGGER.debug("hass_started")
            self._hass_started = True
            self._register_for_weather_updates()

        if hass.state == CoreState.running:
            _LOGGER.debug("hass is already running")
            hass_started()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, hass_started)

        @callback
        def weather_state_changed(event: Event[EventStateChangedData]) -> None:
            old_state = event.data["old_state"]
            new_state = event.data["new_state"]
            old_state = old_state.state if old_state else None
            new_state = new_state.state if new_state else None
            if (old_state in UNAVAILABLE_STATES) != (new_state in UNAVAILABLE_STATES):
                msg = "weather entity %s availability changed from: '%s' to: '%s'"
                _LOGGER.debug(msg, WEATHER_ENTITY, old_state, new_state)

            if (
                old_state in UNAVAILABLE_STATES
                and new_state not in UNAVAILABLE_STATES
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

    @callback
    def _register_for_weather_updates(self):
        _LOGGER.debug("_register_for_weather_updates")
        component: EntityComponent[WeatherEntity] = self.hass.data[WEATHER]
        entity: WeatherEntity = component.get_entity(WEATHER_ENTITY)
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
    def _unregister_weather_updates(self):
        _LOGGER.debug("_unregister_weather_updates cb: %s", self._unsubscribe_callback)
        if self._unsubscribe_callback:
            self._unsubscribe_callback()

    @callback
    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        """Listen for data updates."""
        _LOGGER.debug("async_add_listener")
        if self._shutdown_requested:
            return None

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
        for update_callback, _ in list(self._listeners.values()):
            update_callback()

    @callback
    def _shutdown(self) -> None:
        """Unregister from weather updates and ignore any incoming updates."""
        _LOGGER.debug("_shutdown")
        self._shutdown_requested = True
        self._unregister_weather_updates()
