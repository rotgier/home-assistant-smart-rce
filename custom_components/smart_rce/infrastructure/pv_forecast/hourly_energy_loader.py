"""HourlyEnergyLoader — driven adapter for hourly PV / consumption energy.

Two reads from the HA recorder:
- consumption without water heating per hour for past days, from the hourly
  long-term statistics (kept indefinitely — any number of days back);
- PV and that consumption per hour for today, from the 5-minute statistics
  (the hourly ones lag up to an hour behind the clock).

Both meters are utility meters resetting every 30 min; the statistics' `change`
is reset-aware, so summing it per hour gives the hour's energy.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
import logging
from typing import Any, Final

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_PV_ENTITY: Final = "sensor.total_pv_generation_bi_hourly"
_CONSUMPTION_ENTITY: Final = "sensor.total_consumption_minus_bi_hourly"
# An hour's change outside this range is a meter glitch, not energy.
_MAX_HOURLY_KWH: Final = 15.0
# A past day with fewer hours than this is a gap in the data, not a profile.
_MIN_HOURS_PER_DAY: Final = 20

_LOGGER = logging.getLogger(__name__)


class HourlyEnergyLoader:
    """Loads hourly PV and consumption energy (kWh) from the HA recorder."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def consumption_by_day(
        self, days: list[date]
    ) -> dict[date, dict[int, float]]:
        """Hourly consumption without water heating for each of `days`.

        Days with gaps (fewer than `_MIN_HOURS_PER_DAY` hours) are left out.
        """
        if not days:
            return {}
        per_day = await self._hourly(
            _CONSUMPTION_ENTITY, min(days), max(days) + timedelta(days=1), "hour"
        )
        wanted = set(days)
        return {
            d: hours
            for d, hours in per_day.items()
            if d in wanted and len(hours) >= _MIN_HOURS_PER_DAY
        }

    async def today_by_hour(
        self, today: date
    ) -> tuple[dict[int, float], dict[int, float]]:
        """PV and consumption without water heating per hour so far today."""
        end = today + timedelta(days=1)
        pv = await self._hourly(_PV_ENTITY, today, end, "5minute")
        cons = await self._hourly(_CONSUMPTION_ENTITY, today, end, "5minute")
        return pv.get(today, {}), cons.get(today, {})

    async def _hourly(
        self, statistic_id: str, first: date, end: date, period: str
    ) -> dict[date, dict[int, float]]:
        tz = dt_util.DEFAULT_TIME_ZONE
        start_dt = datetime.combine(first, time(0, 0), tzinfo=tz)
        end_dt = datetime.combine(end, time(0, 0), tzinfo=tz)
        stats: dict[str, list[Any]] = await get_instance(
            self._hass
        ).async_add_executor_job(
            statistics_during_period,
            self._hass,
            start_dt,
            end_dt,
            {statistic_id},
            period,
            None,
            {"change"},
        )
        per_day: dict[date, dict[int, float]] = {}
        for row in stats.get(statistic_id, []):
            raw_start, change = row.get("start"), row.get("change")
            if raw_start is None or change is None:
                continue
            kwh = float(change)
            if not 0.0 <= kwh <= _MAX_HOURLY_KWH:
                continue
            ts = datetime.fromtimestamp(float(raw_start), tz=UTC).astimezone(tz)
            hours = per_day.setdefault(ts.date(), {})
            hours[ts.hour] = hours.get(ts.hour, 0.0) + kwh
        _LOGGER.debug(
            "HourlyEnergyLoader: %s %s %s..%s -> %d days",
            statistic_id,
            period,
            first,
            end,
            len(per_day),
        )
        return per_day
