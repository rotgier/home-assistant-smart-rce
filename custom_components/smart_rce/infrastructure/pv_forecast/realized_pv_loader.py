"""RealizedPvLoader — driven adapter for today's realized PV per 30-min bucket.

Fetches `sensor.total_pv_generation_bi_hourly` history for the current day
through the recorder LTS API. The utility meter resets at :00 / :30, so the
state value just before reset (last 5-min slot at :25 / :55) equals the total
kWh generated in that bucket.

Used by the calibrated_pattern extrapolation variant — needs realized values
of past buckets in current day to compute the realization factor (see
domain/pv_forecast_extrapolation.py).

Hexagonal pattern: **driven adapter (outbound)** — application service dictates
"give me today's realized PV per bucket", concrete impl uses HA recorder.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
import logging
from typing import Final

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_PV_BUCKET_KWH_ENTITY: Final = "sensor.total_pv_generation_bi_hourly"

_LOGGER = logging.getLogger(__name__)


class RealizedPvLoader:
    """Loads today's realized PV per 30-min bucket from HA recorder."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def fetch_today(self, today: date) -> dict[tuple[int, int], float]:
        """Fetch realized PV per (hour, minute) bucket for today (kWh).

        Buckets indexed by their start: (hour, 0) for HH:00-HH:30 cycle,
        (hour, 30) for HH:30-(HH+1):00 cycle. Returns dict from bucket start
        to total kWh in that completed cycle. Buckets not yet closed are
        excluded (utility meter still accumulating).
        """
        tz = dt_util.DEFAULT_TIME_ZONE
        start = datetime.combine(today, time(0, 0), tzinfo=tz)
        end = datetime.combine(today, time(23, 59), tzinfo=tz)

        instance = get_instance(self._hass)
        stats = await instance.async_add_executor_job(
            statistics_during_period,
            self._hass,
            start,
            end,
            {_PV_BUCKET_KWH_ENTITY},
            "5minute",
            None,
            {"state"},
        )
        slots = stats.get(_PV_BUCKET_KWH_ENTITY, [])

        buckets: dict[tuple[int, int], float] = {}
        for slot in slots:
            raw_start = slot.get("start")
            if raw_start is None:
                continue
            ts = datetime.fromtimestamp(float(raw_start), tz=UTC).astimezone(tz)
            if ts.date() != today:
                continue
            state_val = slot.get("state")
            if state_val is None:
                continue
            try:
                value = float(state_val)
            except (TypeError, ValueError):
                continue
            # Last 5-min slot before reset captures full-bucket total.
            if ts.minute == 25:
                buckets[(ts.hour, 0)] = value
            elif ts.minute == 55:
                buckets[(ts.hour, 30)] = value
        _LOGGER.debug(
            "RealizedPvLoader: fetched %d closed buckets for %s", len(buckets), today
        )
        return buckets
