"""EnergyArchiveWriter — copies finished days of 5-minute energy statistics to the archive.

Runs nightly and at startup. Each run archives every finished day the recorder
still holds in full and the archive lacks, so a day missed while HA was down is
caught up on the next start. A complete archived day is never rewritten. An
incomplete one is — but only by a copy that loses nothing (`improves_on`): a
start just after midnight archives yesterday before its 23:55 statistic is
compiled, and the 00:15 run then fills it in.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.util import dt as dt_util

from ..domain.energy_day import EnergyDay

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .daily_jsonl_archive import DailyJsonlArchive

# Statistic id → (series name, statistics field). The *_bi_hourly utility
# meters reset every 30 min; `change` is reset-aware energy per 5-min slot.
ENERGY_SERIES: Final[dict[str, tuple[str, str]]] = {
    "sensor.total_pv_generation_bi_hourly": ("pv_kwh", "change"),
    "sensor.total_consumption_bi_hourly": ("consumption_kwh", "change"),
    "sensor.total_consumption_minus_bi_hourly": (
        "consumption_minus_water_kwh",
        "change",
    ),
    "sensor.total_export_import_bi_hourly": ("grid_net_export_kwh", "change"),
    "sensor.battery_state_of_charge": ("battery_soc_pct", "mean"),
}
# Recorder keeps 10 days; the 10th is already cut by the nightly purge (04:12),
# so only the 9 most recent finished days are complete.
_COMPLETE_DAYS_IN_RECORDER: Final = 9

_LOGGER = logging.getLogger(__name__)


class EnergyArchiveWriter:
    """Archives finished days of measured energy, one file per day."""

    def __init__(self, hass: HomeAssistant, archive: DailyJsonlArchive) -> None:
        self._hass = hass
        self._archive = archive

    async def async_catch_up(self, today: date) -> list[date]:
        """Archive every complete day before `today` that is not archived yet."""
        written: list[date] = []
        for back in range(_COMPLETE_DAYS_IN_RECORDER, 0, -1):
            day = today - timedelta(days=back)
            exists, archived = await self._hass.async_add_executor_job(
                self._archived, day
            )
            if exists and (archived is None or archived.is_complete()):
                continue  # complete, or not ours to rewrite
            energy = await self._fetch_day(day)
            if not energy.has_data():
                continue  # the recorder has nothing for that day
            if archived is not None and not energy.improves_on(archived):
                continue
            await self._hass.async_add_executor_job(
                self._archive.write_day, day, [energy.to_dict()]
            )
            written.append(day)
        if written:
            _LOGGER.info("Energy archive: wrote %s", [d.isoformat() for d in written])
        return written

    def _archived(self, day: date) -> tuple[bool, EnergyDay | None]:
        """Whether `day` has a file, and its energy record when it has one."""
        if not self._archive.has_day(day):
            return False, None
        try:
            return True, EnergyDay.from_records(self._archive.read_day(day))
        except Exception:  # noqa: BLE001 — leave a broken file for a human
            _LOGGER.exception("Energy archive: %s unreadable, left as is", day)
            return True, None

    async def _fetch_day(self, day: date) -> EnergyDay:
        return await fetch_energy_day(self._hass, day)


async def fetch_energy_day(hass: HomeAssistant, day: date) -> EnergyDay:
    """`day` from the recorder's 5-minute statistics — as far as they reach now.

    The archive writer stores finished days with it; the forecast scoreboard
    reads today with it, before the day is archived.
    """
    tz = dt_util.get_default_time_zone()
    start = datetime.combine(day, time(0), tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        set(ENERGY_SERIES),
        "5minute",
        None,
        {"change", "mean"},
    )
    readings: dict[str, dict[datetime, float | None]] = {
        name: _by_slot_start(stats.get(statistic_id, []), field)
        for statistic_id, (name, field) in ENERGY_SERIES.items()
    }
    return EnergyDay.from_readings(day, tz, readings)


def _by_slot_start(rows: list[Any], field: str) -> dict[datetime, float | None]:
    return {
        datetime.fromtimestamp(float(row["start"]), tz=UTC): row.get(field)
        for row in rows
    }
