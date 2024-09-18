"""The Smart RCE coordinator."""

from asyncio import timeout
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.dt import now as now_local

from .const import DOMAIN
from .infrastructure.rce_api import RceApi, RceDayPrices

RCE_TOMORROW_PUBLICATION_HOUR: Final[int] = 14
TIME_CHANGE_MINUTES_PATTERN: Final[str] = "/1"
MINIMUM_TIME_BETWEEN_FETCHES_SECONDS: Final[int] = 14 * 60


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
            name=DOMAIN,
            identifiers={(DOMAIN, entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
        )

        super().__init__(hass, _LOGGER, name=entry.title, always_update=False)

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
            elapsed_seconds = (now - self.data.fetched_at).total_seconds()
            if elapsed_seconds > MINIMUM_TIME_BETWEEN_FETCHES_SECONDS:
                return RceData(
                    fetched_at=now,
                    today=self.data.today,
                    tomorrow=await self._fetch_prices_for_day(now + timedelta(days=1)),
                )
        return self.data

    async def _full_update(self, now: datetime) -> RceData:
        return RceData(
            fetched_at=now,
            today=await self._fetch_prices_for_day(now),
            tomorrow=await self._fetch_prices_for_day(now + timedelta(days=1)),
        )

    async def _fetch_prices_for_day(self, day: datetime) -> RceDayPrices:
        try:
            async with timeout(10):
                result = await self._rce_api.async_get_prices(day)
        except Exception as error:
            _LOGGER.exception("Update failed")
            raise UpdateFailed(error) from error

        return result

    async def async_shutdown(self) -> None:
        """Add track time change cancelation."""
        await super().async_shutdown()
        self._cancel_track_time_change()

    @callback
    def _schedule_refresh(self) -> None:
        """Subscribe to time changes when first listener is added."""
        super()._schedule_refresh()

        async def async_refresh_with_datetime(now: datetime) -> None:
            await self.async_refresh()

        if not self._shutdown_requested and not self._cancel_track_time_change_cb:
            self._cancel_track_time_change_cb = async_track_time_change(
                self.hass,
                async_refresh_with_datetime,
                second=0,
                minute=TIME_CHANGE_MINUTES_PATTERN,
            )

    @callback
    def _unschedule_refresh(self) -> None:
        """Unsubscribe from time changes when last listener is removed."""
        super()._unschedule_refresh()
        if not self._listeners:
            self._cancel_track_time_change()

    def _cancel_track_time_change(self) -> None:
        if self._cancel_track_time_change_cb:
            self._cancel_track_time_change_cb()
        self._cancel_track_time_change_cb = None
