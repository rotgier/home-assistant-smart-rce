"""Monthly PV production from the recorder's long-term statistics.

Only needed for the year-on-year comparison, so it reads whole months rather
than hours — long-term statistics keep monthly sums forever, which makes the
whole measured era a single cheap query.

The sensor was created late in September 2024; everything before that comes from
the inverter's own history, shipped in the seed.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Final

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.util import dt as dt_util

from ..domain.billing_month import BillingMonth

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

_PRODUCTION_ENTITY: Final = "sensor.total_pv_generation_hourly"

_LOGGER = logging.getLogger(__name__)


class PvProductionReader:
    """Reads monthly PV generation from HA recorder."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def async_months_between(
        self, start: datetime.date, end: datetime.date
    ) -> Mapping[BillingMonth, float]:
        """Return generated kWh per calendar month over [start, end]."""
        tz = dt_util.DEFAULT_TIME_ZONE
        stats = await get_instance(self._hass).async_add_executor_job(
            statistics_during_period,
            self._hass,
            datetime.datetime.combine(start, datetime.time.min, tzinfo=tz),
            datetime.datetime.combine(end, datetime.time.max, tzinfo=tz),
            {_PRODUCTION_ENTITY},
            "month",
            None,
            {"change"},
        )
        per_month: dict[BillingMonth, float] = {}
        for row in stats.get(_PRODUCTION_ENTITY, []):
            raw_start, change = row.get("start"), row.get("change")
            if raw_start is None or change is None:
                continue
            moment = datetime.datetime.fromtimestamp(
                float(raw_start), tz=datetime.UTC
            ).astimezone(tz)
            per_month[BillingMonth(moment.year, moment.month)] = float(change)
        _LOGGER.debug("PvProductionReader: %d months measured", len(per_month))
        return per_month
