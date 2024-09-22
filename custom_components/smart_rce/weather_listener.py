"""Weather Listener Coordinator."""

from collections.abc import Callable
from datetime import date, datetime
import logging
from typing import Final

import aiofiles
import orjson

from homeassistant.components.weather import (
    ATTR_FORECAST_TIME,
    DOMAIN as WEATHER,
    Forecast,
    WeatherEntity,
)
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
from homeassistant.util.dt import as_local, now as now_local
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

        self._last_hourly_forecast: list[Forecast] = None
        self._last_hourly_forecast_day: date = None

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
                    self._has_hourly_forecast_changed(forecast)
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

    def _has_hourly_forecast_changed(self, forecast: list[Forecast]) -> bool:
        now = now_local()

        first_hour_raw = forecast[0][ATTR_FORECAST_TIME]
        forecast_first_hour: datetime = as_local(datetime.fromisoformat(first_hour_raw))
        forecast_day: date = forecast_first_hour.date()

        if not self._last_hourly_forecast:
            _LOGGER.debug("Save forecast because no last forecast")
            self._save_forecast_to_file(now, forecast, forecast_day)
            return True

        last_hourly_forecast = self._last_hourly_forecast
        len_difference = len(last_hourly_forecast) - len(forecast)

        assert self._last_hourly_forecast_day
        if forecast_day != self._last_hourly_forecast_day:
            _LOGGER.debug("Save forecast because day differs")
            self._save_forecast_to_file(now, forecast, forecast_day)
            return True

        if len_difference == 0:
            if forecast != last_hourly_forecast:
                _LOGGER.debug("Save forecast SAME size because it differs")
                self._save_forecast_to_file(now, forecast, forecast_day)
                return True
            _LOGGER.debug("NO Save forecast SAME size because it is the same")
            return False
        if len_difference > 0:
            if (
                last_hourly_forecast[len_difference][ATTR_FORECAST_TIME]
                != forecast[0][ATTR_FORECAST_TIME]
            ):
                _LOGGER.warning("First element datetime does not match!")
            if (
                last_hourly_forecast[len_difference + 1][ATTR_FORECAST_TIME]
                != forecast[1][ATTR_FORECAST_TIME]
            ):
                _LOGGER.warning("Second element datetime does not match!")
            if forecast != last_hourly_forecast[len_difference:]:
                _LOGGER.warning("Save forecast SMALLER size because it differs")
                self._save_forecast_to_file(now, forecast, forecast_day)
            else:
                _LOGGER.warning("New SMALLER hourly_forecast is the same")
            return True
        _LOGGER.warning("Save forecast BIGGER size!!! BIGGER means sth is wrong")
        _LOGGER.warning("BIGGER len_difference: %d", len_difference)
        self._save_forecast_to_file(now, forecast, forecast_day)
        return True

    def _save_forecast_to_file(
        self, now: datetime, forecast: list[Forecast], forecast_day: date
    ) -> None:
        self._last_hourly_forecast = forecast
        self._last_hourly_forecast_day = forecast_day
        _LOGGER.debug("_save_forecast_to_file")
        self.hass.loop.create_task(self._async_save_forecast_to_file(now, forecast))

    async def _async_save_forecast_to_file(
        self, now: datetime, forecast: list[Forecast]
    ) -> None:
        _LOGGER.debug("_async_save_forecast_to_file")
        raw_json = orjson.dumps(forecast, option=orjson.OPT_INDENT_2)
        path = f"/config/smart_rce/hourly_forecast_{now.isoformat()}.json"
        async with aiofiles.open(path, mode="w+", encoding="utf-8") as file:
            await file.write(raw_json.decode("utf-8"))
