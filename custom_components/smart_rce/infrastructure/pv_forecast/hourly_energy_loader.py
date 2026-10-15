"""HourlyEnergyLoader — driven adapter for PV / consumption energy per 30-min slot.

Two reads:
- consumption without water heating per 30-min slot for past days: from the
  energy archive (5-minute resolution kept indefinitely), and for a day it
  lacks from the hourly long-term statistics, each hour split evenly over its
  two slots (the shape within the hour is lost there);
- PV and that consumption per 30-min slot for today, from the 5-minute
  statistics (the hourly ones lag up to an hour behind the clock).

Both meters are utility meters resetting every 30 min; the statistics' `change`
is reset-aware, so summing it per hour (or slot) gives that period's energy.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ...domain.pv_surplus import SLOT_MINUTES, slots_from_hourly

if TYPE_CHECKING:
    from ..energy_archive_reader import EnergyArchiveReader

_PV_ENTITY: Final = "sensor.total_pv_generation_bi_hourly"
_CONSUMPTION_ENTITY: Final = "sensor.total_consumption_minus_bi_hourly"
# The same meter's series in the energy archive.
_ARCHIVE_SERIES: Final = "consumption_minus_water_kwh"
# A 5-min or hourly change outside this range is a meter glitch, not energy.
_MAX_HOURLY_KWH: Final = 15.0
# A past day with fewer hours than this is a gap in the data, not a profile.
_MIN_HOURS_PER_DAY: Final = 20
_MIN_SLOTS_PER_DAY: Final = _MIN_HOURS_PER_DAY * 60 // SLOT_MINUTES

_LOGGER = logging.getLogger(__name__)


class HourlyEnergyLoader:
    """Loads hourly PV and consumption energy (kWh) from the HA recorder."""

    def __init__(self, hass: HomeAssistant, archive: EnergyArchiveReader) -> None:
        self._hass = hass
        self._archive = archive

    async def consumption_by_day(
        self, days: list[date]
    ) -> dict[date, dict[int, float]]:
        """Consumption without water heating per 30-min slot for each of `days`.

        Days with gaps (fewer than 20 hours' worth of slots) are left out.
        """
        if not days:
            return {}
        tz = dt_util.DEFAULT_TIME_ZONE
        found: dict[date, dict[int, float]] = {}
        for day, energy in (await self._archive.read_days(days)).items():
            slots = energy.energy_by_half_hour(_ARCHIVE_SERIES, tz)
            if len(slots) >= _MIN_SLOTS_PER_DAY:
                found[day] = slots
        missing = [d for d in days if d not in found]
        if missing:
            found.update(await self._hourly_by_day(missing))
        return found

    async def _hourly_by_day(self, days: list[date]) -> dict[date, dict[int, float]]:
        """Fallback: hourly long-term statistics, each hour split over two slots."""
        per_day = await self._by_day(
            _CONSUMPTION_ENTITY, min(days), max(days) + timedelta(days=1), "hour", 60
        )
        wanted = set(days)
        return {
            d: slots_from_hourly(hours)
            for d, hours in per_day.items()
            if d in wanted and len(hours) >= _MIN_HOURS_PER_DAY
        }

    async def today_by_slot(
        self, today: date
    ) -> tuple[dict[int, float], dict[int, float]]:
        """PV and consumption without water heating per 30-min slot so far today."""
        end = today + timedelta(days=1)
        pv = await self._by_day(_PV_ENTITY, today, end, "5minute", SLOT_MINUTES)
        cons = await self._by_day(
            _CONSUMPTION_ENTITY, today, end, "5minute", SLOT_MINUTES
        )
        return pv.get(today, {}), cons.get(today, {})

    async def _by_day(
        self,
        statistic_id: str,
        first: date,
        end: date,
        period: str,
        bucket_minutes: int,
    ) -> dict[date, dict[int, float]]:
        """Energy per day, summed into buckets of `bucket_minutes` (key = index)."""
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
            buckets = per_day.setdefault(ts.date(), {})
            key = (ts.hour * 60 + ts.minute) // bucket_minutes
            buckets[key] = buckets.get(key, 0.0) + kwh
        _LOGGER.debug(
            "HourlyEnergyLoader: %s %s %s..%s -> %d days",
            statistic_id,
            period,
            first,
            end,
            len(per_day),
        )
        return per_day
