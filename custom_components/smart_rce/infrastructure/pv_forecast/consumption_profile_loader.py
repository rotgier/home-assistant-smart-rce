"""ConsumptionProfileLoader — driven adapter for HA recorder LTS query.

Fetches prev-workday consumption profiles from `sensor.total_consumption_minus_bi_hourly`
through the recorder LTS API. Walks back PREV_DAYS_COUNT actual workdays
(holiday-aware — set sourced from `WorkdayCalendarReader`, see
`walk_back_workdays` in domain/pv_forecast.py), batched in a single
async query, bucketed per (date, half-hour).

Hexagonal pattern: **driven adapter (outbound)** — application service dictates
"give me consumption profiles for the last N workdays", the concrete impl uses
HA recorder.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
import logging
from typing import Final

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ...domain import pv_forecast
from ...domain.pv_forecast import ConsumptionProfile
from ...domain.target_soc import CONSUMPTION_PER_30MIN
from ..workday_calendar_reader import WorkdayCalendarReader

_CONSUMPTION_SENSOR_ID: Final = "sensor.total_consumption_minus_bi_hourly"
# All 12 buckets covering 7:00..12:30 — used to fill the strict
# ConsumptionProfile contract when a workday's recorder data is partial
# (sensor gaps, restarts, etc.). Domain default `CONSUMPTION_PER_30MIN` is
# the same baseline as the synthetic "live" profile.
_DEFAULT_BUCKETS: Final[dict[tuple[int, int], float]] = {
    (h, m): CONSUMPTION_PER_30MIN for h in range(7, 13) for m in (0, 30)
}

_LOGGER = logging.getLogger(__name__)


class ConsumptionProfileLoader:
    """Fetches PREV_DAYS_COUNT prev-workday consumption profiles from HA recorder."""

    def __init__(
        self,
        hass: HomeAssistant,
        workday_reader: WorkdayCalendarReader,
    ) -> None:
        self._hass = hass
        self._workday_reader = workday_reader

    async def fetch(self, today: date) -> list[ConsumptionProfile | None]:
        """Fetch profiles for the last PREV_DAYS_COUNT workdays before `today`.

        Delegates to `fetch_for_anchor` with the canonical anchor=today and
        the domain-configured count. Used by the per-sensor target_soc
        recomputation path.
        """
        return await self.fetch_for_anchor(today, pv_forecast.PREV_DAYS_COUNT)

    async def fetch_for_anchor(
        self, anchor: date, count: int
    ) -> list[ConsumptionProfile | None]:
        """Fetch `count` prev-workday profiles strictly before `anchor` (single LTS query).

        Walk back N actual workdays (calendar-driven, holiday-aware) from
        the given anchor, compute earliest..latest date span, fetch 5-min
        stats once, bucket per date in memory.

        Used by the target_soc matrix to anchor the "prev workday" walk
        at the date-picker target instead of today — when the user
        inspects tomorrow's matrix, Prev 1 should be today (if today is a
        workday) rather than yesterday's workday.
        """
        workday_dates = await self._workday_reader.fetch_workdays(anchor)
        if not workday_dates:
            _LOGGER.warning(
                "Workday calendar returned no events — prev-day consumption "
                "profiles unavailable; check calendar.workday_calendar config"
            )
            return [None] * count

        dates: list[date | None] = [
            pv_forecast.walk_back_workdays(anchor, i + 1, workday_dates)
            for i in range(count)
        ]
        valid_dates = [d for d in dates if d is not None]
        if not valid_dates:
            _LOGGER.debug("No valid prev workdays found before %s", anchor)
            return [None] * count

        slots = await self._fetch_5min_slots(valid_dates)
        return self._bucket_profiles_by_date(slots, dates, valid_dates)

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

    @staticmethod
    def _bucket_profiles_by_date(
        slots: list[dict], dates: list[date | None], valid_dates: list[date]
    ) -> list[ConsumptionProfile | None]:
        """Bucket 5-min slots by (date, half-hour) → ConsumptionProfile per date.

        Utility_meter resets at :00 and :30 — last pre-reset slot is :25 and :55.
        State value in that slot = total consumption in the 30-min cycle.
        Bucket (hour, 0)  = state in slot (hour, 25)
        Bucket (hour, 30) = state in slot (hour, 55)
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
            ConsumptionProfile(
                buckets={**_DEFAULT_BUCKETS, **by_date[d]}, source_date=d
            )
            if d and by_date.get(d)
            else None
            for d in dates
        ]
