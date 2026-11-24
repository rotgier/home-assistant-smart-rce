"""WeatherArchiveWriter — copies finished days of observed weather to the archive.

The recorder keeps states for 10 days; the heating analyses need the wind and
the outside temperature for longer (user's request 24.11.2026). Runs nightly
and at startup, like the energy archive: every finished day the recorder still
holds in full and the archive lacks is written once (the first start backfills
9 days). An archived day is never rewritten.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import logging
from typing import TYPE_CHECKING, Final

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.util import dt as dt_util

from ..domain.weather_observation import (
    OUTSIDE_TEMP,
    WIND_BEARING,
    WIND_GUST,
    WIND_SPEED,
    WO_APPARENT,
    WO_DEW_POINT,
    WO_HUMIDITY,
    WO_PRECIPITATION_TYPE,
    WO_PRESSURE,
    WO_TEMPERATURE,
    WO_VISIBILITY,
    ObservedSeries,
    ObservedWeatherDay,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, State

    from .daily_jsonl_archive import DailyJsonlArchive

WEATHER_OBSERVED_DIR: Final = "smart_rce/archive/weather_observed"
# Role -> entity. The house's boiler sensor is the outside temperature measured
# here; WetterOnline's values are a model next to it (agreed with the heating
# analyses 24.11.2026).
ENTITIES: Final[dict[str, str]] = {
    WIND_SPEED: "sensor.wetteronline_wind_speed",
    WIND_BEARING: "sensor.wetteronline_wind_bearing",
    WIND_GUST: "sensor.wetteronline_wind_gust",  # from 24.11.2026
    OUTSIDE_TEMP: "sensor.vitodens_outside_temp",
    WO_TEMPERATURE: "sensor.wetteronline_temperature",
    WO_APPARENT: "sensor.wetteronline_apparent_temperature",
    WO_HUMIDITY: "sensor.wetteronline_humidity",
    WO_DEW_POINT: "sensor.wetteronline_dew_point",
    WO_PRESSURE: "sensor.wetteronline_pressure",
    WO_VISIBILITY: "sensor.wetteronline_visibility",
    WO_PRECIPITATION_TYPE: "sensor.wetteronline_precipitation_type",
}
# Roles whose state is text, kept as recorded (the others are numbers).
TEXT_ROLES: Final = frozenset({WO_PRECIPITATION_TYPE})
_NO_VALUE: Final = frozenset({"unknown", "unavailable", ""})

_LOGGER = logging.getLogger(__name__)


class WeatherArchiveWriter:
    """Archives finished days of observed weather, one file per day."""

    # Recorder keeps 10 days; the 10th is already cut by the nightly purge
    # (04:12), so only the 9 most recent finished days are complete.
    _COMPLETE_DAYS_IN_RECORDER: Final = 9

    def __init__(self, hass: HomeAssistant, archive: DailyJsonlArchive) -> None:
        self._hass = hass
        self._archive = archive

    async def async_catch_up(self, today: date) -> list[date]:
        """Archive every complete day before `today` that is not archived yet."""
        written: list[date] = []
        for back in range(self._COMPLETE_DAYS_IN_RECORDER, 0, -1):
            day = today - timedelta(days=back)
            if await self._hass.async_add_executor_job(self._archive.has_day, day):
                continue
            observed = await self._fetch_day(day)
            if not any(
                s.points or s.in_force_at_start is not None
                for s in observed.series.values()
            ):
                continue  # the recorder has nothing for that day
            await self._hass.async_add_executor_job(
                self._archive.write_day, day, observed.to_records()
            )
            written.append(day)
        if written:
            _LOGGER.info("Weather archive: wrote %s", [d.isoformat() for d in written])
        return written

    async def _fetch_day(self, day: date) -> ObservedWeatherDay:
        """Read the day's state changes of every entity (the state at midnight too)."""
        tz = dt_util.get_default_time_zone()
        start = datetime.combine(day, time(0), tz)
        end = datetime.combine(day + timedelta(days=1), time(0), tz)
        recorder = get_instance(self._hass)
        series: dict[str, ObservedSeries] = {}
        for role, entity_id in ENTITIES.items():
            result = await recorder.async_add_executor_job(
                state_changes_during_period,
                self._hass,
                start,
                end,
                entity_id,
                True,  # no_attributes
            )
            rows = [
                (s.last_changed, self._value(s, role))
                for s in result.get(entity_id, [])
            ]
            current = self._hass.states.get(entity_id)
            unit = current.attributes.get("unit_of_measurement") if current else None
            series[role] = ObservedSeries.of(entity_id, unit, rows, start)
        return ObservedWeatherDay(day, tz, series)

    @staticmethod
    def _value(state: State, role: str) -> float | str | None:
        """Return the state as recorded: a number, text for a text role, None for none."""
        if state.state in _NO_VALUE:
            return None
        if role in TEXT_ROLES:
            return state.state
        try:
            return float(state.state)
        except ValueError:
            return None
