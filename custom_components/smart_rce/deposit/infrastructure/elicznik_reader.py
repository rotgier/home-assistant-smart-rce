"""MeterReadingsProvider backed by the TAURON eLicznik scraper.

The library is synchronous and scrapes a web app, so every call goes to an
executor. It logs in ONCE per call and pulls the whole day range in that session
— TAURON bans on a burst of logins (~12 h), so the refresh must never loop
per-day over this adapter.
"""

from __future__ import annotations

from collections import defaultdict
import datetime
import logging
from typing import TYPE_CHECKING

import elicznik

from ..domain.meter_reading import HourReading

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class ElicznikReader:
    """Reads hourly balanced readings from TAURON eLicznik."""

    def __init__(self, hass: HomeAssistant, username: str, password: str) -> None:
        self._hass = hass
        self._username = username
        self._password = password

    async def async_readings_for(
        self, start: datetime.date, end: datetime.date
    ) -> Mapping[datetime.date, Mapping[int, HourReading]]:
        return await self._hass.async_add_executor_job(self._fetch, start, end)

    def _fetch(
        self, start: datetime.date, end: datetime.date
    ) -> dict[datetime.date, dict[int, HourReading]]:
        """Blocking scrape — one login for the whole range."""
        _LOGGER.debug("eLicznik: fetching %s..%s (single login)", start, end)
        with elicznik.ELicznik(self._username, self._password) as meter:
            readings = meter.get_readings(start, end)
        per_day: dict[datetime.date, dict[int, HourReading]] = defaultdict(dict)
        for timestamp, _consumed, _produced, net_consumed, net_produced in readings:
            per_day[timestamp.date()][timestamp.hour] = HourReading(
                exported_kwh=net_produced or 0.0,
                imported_kwh=net_consumed or 0.0,
            )
        _LOGGER.debug("eLicznik: got %d days", len(per_day))
        return dict(per_day)
