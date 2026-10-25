"""Hourly battery discharge from the recorder's long-term statistics.

Source for splitting the deposit by origin (`ExportOrigin`). One counter,
`sensor.total_battery_discharge`, read as hourly `change`. Long-term statistics
are never purged, so any settled month can be read back.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Final

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.util import dt as dt_util

from .household_energy_reader import _by_slot

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_DISCHARGE_ENTITY: Final = "sensor.total_battery_discharge"


class BatteryDischargeReader:
    """Reads hourly battery discharge (kWh) from HA recorder."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def async_hours_between(
        self, start: datetime.date, end: datetime.date
    ) -> dict[tuple[datetime.date, int], float]:
        """Discharge per (local date, clock hour) for [start, end]."""
        tz = dt_util.DEFAULT_TIME_ZONE
        stats = await get_instance(self._hass).async_add_executor_job(
            statistics_during_period,
            self._hass,
            datetime.datetime.combine(start, datetime.time.min, tzinfo=tz),
            datetime.datetime.combine(end, datetime.time.max, tzinfo=tz),
            {_DISCHARGE_ENTITY},
            "hour",
            None,
            {"change"},
        )
        return _by_slot(stats.get(_DISCHARGE_ENTITY, []), tz)
