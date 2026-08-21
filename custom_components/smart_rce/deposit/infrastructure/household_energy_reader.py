"""Hourly household energy from the recorder's long-term statistics.

Source for self-consumption. Two utility-meter sensors, both hourly `change`:
total house consumption and the hourly-balanced grid net. Long-term statistics
are never purged, so the whole history since the sensors were created stays
available and the whole range can simply be recomputed on every run — cheaper
than getting a watermark subtly wrong, and it costs nothing outside the house.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Final

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    statistics_during_period,
)
from homeassistant.util import dt as dt_util

from ..domain.self_consumption import HouseholdHour

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from homeassistant.core import HomeAssistant

_CONSUMPTION_ENTITY: Final = "sensor.total_consumption_hourly"
_NET_ENTITY: Final = "sensor.total_export_import_hourly"

_LOGGER = logging.getLogger(__name__)


class HouseholdEnergyReader:
    """Reads hourly consumption and balanced net from HA recorder."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def async_hours_between(
        self, start: datetime.date, end: datetime.date
    ) -> Mapping[datetime.date, Mapping[int, HouseholdHour]]:
        """Hourly readings for [start, end], keyed by local date and clock hour."""
        tz = dt_util.DEFAULT_TIME_ZONE
        stats = await get_instance(self._hass).async_add_executor_job(
            statistics_during_period,
            self._hass,
            datetime.datetime.combine(start, datetime.time.min, tzinfo=tz),
            datetime.datetime.combine(end, datetime.time.max, tzinfo=tz),
            {_CONSUMPTION_ENTITY, _NET_ENTITY},
            "hour",
            None,
            {"change"},
        )
        consumption = _by_slot(stats.get(_CONSUMPTION_ENTITY, []), tz)
        net = _by_slot(stats.get(_NET_ENTITY, []), tz)
        per_day: dict[datetime.date, dict[int, HouseholdHour]] = {}
        for slot, consumed in consumption.items():
            if slot not in net:
                continue  # no matching balanced hour — cannot tell import apart
            day, hour = slot
            per_day.setdefault(day, {})[hour] = HouseholdHour(
                consumption_kwh=max(0.0, consumed), net_kwh=net[slot]
            )
        _LOGGER.debug(
            "HouseholdEnergyReader: %d days between %s and %s",
            len(per_day),
            start,
            end,
        )
        return per_day


def _by_slot(
    rows: Sequence[StatisticsRow], tz: datetime.tzinfo
) -> dict[tuple[datetime.date, int], float]:
    """Index statistic rows by (local date, clock hour)."""
    indexed: dict[tuple[datetime.date, int], float] = {}
    for row in rows:
        raw_start, change = row.get("start"), row.get("change")
        if raw_start is None or change is None:
            continue
        moment = datetime.datetime.fromtimestamp(
            float(raw_start), tz=datetime.UTC
        ).astimezone(tz)
        indexed[(moment.date(), moment.hour)] = float(change)
    return indexed
