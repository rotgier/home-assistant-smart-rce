"""The Smart RCE coordinator."""

from asyncio import timeout
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any, Final

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
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.event import (
    Event,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.dt import now as now_local
from homeassistant.util.json import JsonValueType

from .const import DOMAIN
from .rce_api import RceApi, RceDayPrices

RCE_TOMORROW_PUBLICATION_HOUR: Final[int] = 14
TIME_CHANGE_MINUTES_PATTERN: Final[str] = "/1"
MINIMUM_TIME_BETWEEN_FETCHES_SECONDS: Final[int] = 14 * 60
WEATHER_ENTITY: Final[str] = "weather.wetteronline"
UNAVAILABLE_STATES: Final[tuple[str | None]] = (
    STATE_UNKNOWN,
    STATE_UNAVAILABLE,
    "",
    None,
)


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class RceData:
    """RCE prices data."""

    fetched_at: datetime
    today: RceDayPrices
    tomorrow: RceDayPrices


class SmartRceDataUpdateCoordinator(DataUpdateCoordinator[RceData]):
    """Class to manage fetching WetterOnline data."""

    def __init__(
        self, hass: HomeAssistant, rce_api: RceApi, entry: ConfigEntry
    ) -> None:
        """Initialize."""
        self._rce_api = rce_api
        self._last_rce_data: RceData = None
        self._cancel_track_time_change_cb: CALLBACK_TYPE = None

        self.device_info = DeviceInfo(
            name=entry.title,
            identifiers={(DOMAIN, entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
        )

        super().__init__(hass, _LOGGER, name=entry.title, always_update=False)

    async def _fetch_prices_for_day(self, day: datetime) -> RceDayPrices:
        try:
            async with timeout(10):
                result = await self._rce_api.async_get_prices(day)
        except Exception as error:
            _LOGGER.exception("Update failed")
            raise UpdateFailed(error) from error

        return result

    async def _full_update(self, now: datetime) -> RceData:
        return RceData(
            fetched_at=now,
            today=await self._fetch_prices_for_day(now),
            tomorrow=await self._fetch_prices_for_day(now + timedelta(days=1)),
        )

    async def _async_update_data(self) -> RceData:
        """Update data via library."""
        now = now_local()
        if not self.data or not self.data.today:
            return await self._full_update(now)
        if self.data.fetched_at.date() != now.date():
            return await self._full_update(now)
        if self.data.tomorrow:
            return self.data
        if now.hour >= RCE_TOMORROW_PUBLICATION_HOUR:
            elapsed_seconds = (now - self.data.fetched_at).total_seconds
            if elapsed_seconds > MINIMUM_TIME_BETWEEN_FETCHES_SECONDS:
                return RceData(
                    fetched_at=now,
                    today=self.data.today,
                    tomorrow=await self._fetch_prices_for_day(now + timedelta(days=1)),
                )
        return self.data

    @callback
    def _schedule_refresh(self) -> None:
        """Subscribe to time changes when first listener is added."""
        super()._schedule_refresh()
        if not self._shutdown_requested and not self._cancel_track_time_change_cb:
            self._cancel_track_time_change_cb = async_track_time_change(
                self.hass,
                self._async_refresh_with_datetime,
                second=0,
                minute=TIME_CHANGE_MINUTES_PATTERN,
            )

    @callback
    def _unschedule_refresh(self) -> None:
        """Unsubscribe from time changes when last listener is removed."""
        super()._unschedule_refresh()
        if not self._listeners:
            self._cancel_track_time_change()

    async def async_shutdown(self) -> None:
        """Add track time change cancelation."""
        await super().async_shutdown()
        self._cancel_track_time_change()

    def _cancel_track_time_change(self) -> None:
        if self._cancel_track_time_change_cb:
            self._cancel_track_time_change_cb()
        self._cancel_track_time_change_cb = None

    async def _async_refresh_with_datetime(self, now: datetime) -> None:
        await self.async_refresh()


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
            _LOGGER.debug("Trigger async_update_listeners")
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
