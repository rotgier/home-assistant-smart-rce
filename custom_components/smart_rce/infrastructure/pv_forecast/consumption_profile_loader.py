"""ConsumptionProfileLoader — driven adapter dla HA recorder LTS query.

Fetches prev-workday consumption profiles z `sensor.total_consumption_minus_bi_hourly`
przez recorder LTS API. Walks back PREV_DAYS_COUNT workdays (skip weekends —
domain decision, see `walk_back_workdays` w domain/pv_forecast.py), batches
w jednym async query, buckets per (date, half-hour).

Hexagonal pattern: **driven adapter (outbound)** — application service dictates
"give me consumption profiles for last N workdays", konkretna impl używa HA
recorder.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
import logging
from typing import Final

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ...domain.pv_forecast import (
    PREV_DAYS_COUNT,
    ConsumptionProfile,
    walk_back_workdays,
)

_CONSUMPTION_SENSOR_ID: Final = "sensor.total_consumption_minus_bi_hourly"

_LOGGER = logging.getLogger(__name__)


class ConsumptionProfileLoader:
    """Fetches PREV_DAYS_COUNT prev-workday consumption profiles z HA recorder."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def fetch(self, today: date) -> list[ConsumptionProfile | None]:
        """Fetch profiles dla last PREV_DAYS_COUNT workdays w SINGLE LTS query.

        Walk back N workdays (skip weekends), compute earliest..latest date span,
        fetch 5-min stats once, then bucket per date in memory.
        """
        dates: list[date | None] = [
            walk_back_workdays(today, i + 1) for i in range(PREV_DAYS_COUNT)
        ]
        valid_dates = [d for d in dates if d is not None]
        if not valid_dates:
            _LOGGER.debug("No valid prev workdays found")
            return [None] * PREV_DAYS_COUNT

        slots = await self._fetch_5min_slots(valid_dates)
        return _bucket_profiles_by_date(slots, dates, valid_dates)

    async def _fetch_5min_slots(self, valid_dates: list[date]) -> list[dict]:
        tz = dt_util.DEFAULT_TIME_ZONE
        earliest, latest = min(valid_dates), max(valid_dates)
        start = datetime.combine(earliest, time(6, 30), tzinfo=tz)
        end = datetime.combine(latest, time(13, 35), tzinfo=tz)

        instance = get_instance(self._hass)
        stats = await instance.async_add_executor_job(
            statistics_during_period,
            self._hass,
            start,
            end,
            {_CONSUMPTION_SENSOR_ID},
            "5minute",
            None,
            {"state"},
        )
        slots = stats.get(_CONSUMPTION_SENSOR_ID, [])
        _LOGGER.debug(
            "Fetched %d 5-min slots for %s between %s and %s",
            len(slots),
            _CONSUMPTION_SENSOR_ID,
            start.date(),
            end.date(),
        )
        return slots


def _bucket_profiles_by_date(
    slots: list[dict], dates: list[date | None], valid_dates: list[date]
) -> list[ConsumptionProfile | None]:
    """Bucket 5-min slots by (date, half-hour) → ConsumptionProfile per date.

    Utility_meter resetuje na :00 i :30 — last pre-reset slot to :25 i :55.
    Value state w tym slocie = total consumption w 30-min cyklu.
    Bucket (hour, 0)  = state w slocie (hour, 25)
    Bucket (hour, 30) = state w slocie (hour, 55)
    """
    tz = dt_util.DEFAULT_TIME_ZONE
    by_date: dict[date, dict[tuple[int, int], float]] = {d: {} for d in valid_dates}
    for slot in slots:
        raw_start = slot.get("start")
        if raw_start is None:
            continue
        ts = datetime.fromtimestamp(float(raw_start), tz=UTC).astimezone(tz)
        d = ts.date()
        if d not in by_date or ts.hour < 7 or ts.hour >= 13:
            continue
        state_val = slot.get("state")
        if state_val is None:
            continue
        try:
            value = float(state_val)
        except (TypeError, ValueError):
            continue
        if ts.minute == 25:
            by_date[d][(ts.hour, 0)] = value
        elif ts.minute == 55:
            by_date[d][(ts.hour, 30)] = value

    return [
        ConsumptionProfile(buckets=dict(by_date[d]), source_date=d)
        if d and by_date.get(d)
        else None
        for d in dates
    ]
