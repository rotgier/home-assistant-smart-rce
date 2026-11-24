"""The weather archive copies finished days of observed weather from the recorder."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from custom_components.smart_rce.domain.weather_observation import (
    OUTSIDE_TEMP,
    WIND_BEARING,
    WIND_SPEED,
    WO_PRECIPITATION_TYPE,
    ObservedWeatherDay,
)
from custom_components.smart_rce.infrastructure.daily_jsonl_archive import (
    DailyJsonlArchive,
)
from custom_components.smart_rce.infrastructure.weather_archive_writer import (
    ENTITIES,
    WeatherArchiveWriter,
)
from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.core import HomeAssistant

TZ = ZoneInfo("Europe/Warsaw")
DAY = date(2026, 11, 23)


async def test_a_finished_day_is_archived_once_with_the_state_at_midnight(
    recorder_mock: Any,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    tmp_path: Path,
) -> None:
    await hass.config.async_set_time_zone("Europe/Warsaw")
    steps = [
        # The evening before: what holds at midnight.
        (datetime(2026, 11, 22, 21, 0, tzinfo=TZ), WIND_SPEED, "18", "km/h"),
        (datetime(2026, 11, 22, 21, 0, tzinfo=TZ), WIND_BEARING, "275", "°"),
        (datetime(2026, 11, 22, 23, 50, tzinfo=TZ), OUTSIDE_TEMP, "-0.5", "°C"),
        (
            datetime(2026, 11, 22, 23, 50, tzinfo=TZ),
            WO_PRECIPITATION_TYPE,
            "rain",
            None,
        ),
        # The day.
        (datetime(2026, 11, 23, 5, 30, tzinfo=TZ), WIND_BEARING, "350", "°"),
        (datetime(2026, 11, 23, 6, 15, tzinfo=TZ), OUTSIDE_TEMP, "unavailable", "°C"),
        (datetime(2026, 11, 23, 6, 45, tzinfo=TZ), OUTSIDE_TEMP, "-1.0", "°C"),
        (datetime(2026, 11, 23, 8, 0, tzinfo=TZ), WO_PRECIPITATION_TYPE, "snow", None),
    ]
    for moment, role, state, unit in steps:
        freezer.move_to(moment)
        attrs = {"unit_of_measurement": unit} if unit else {}
        hass.states.async_set(ENTITIES[role], state, attrs)
        await hass.async_block_till_done()
    freezer.move_to(datetime(2026, 11, 24, 0, 25, tzinfo=TZ))
    await async_wait_recording_done(hass)

    archive = DailyJsonlArchive(tmp_path)
    writer = WeatherArchiveWriter(hass, archive)
    assert await writer.async_catch_up(date(2026, 11, 24)) == [
        date(2026, 11, 22),
        DAY,
    ]
    day = ObservedWeatherDay.from_records(DAY, TZ, archive.read_day(DAY))
    speed = day.series[WIND_SPEED]
    assert (speed.in_force_at_start, speed.points, speed.unit) == (18.0, (), "km/h")
    temp = day.series[OUTSIDE_TEMP]
    assert temp.in_force_at_start == -0.5
    assert [p.value for p in temp.points] == [None, -1.0]
    assert day.series[WO_PRECIPITATION_TYPE].points[0].value == "snow"
    hours = {h.start.hour: h for h in day.hours()}
    # 275° for 30 min, 350° for 30 at the same speed: 312.5°, north-west.
    assert (hours[5].wind.direction_deg, hours[5].wind.direction) == (312, "NW")
    assert hours[6].measures[OUTSIDE_TEMP].cover_min == 30  # the outage is no value
    rows = [r for r in archive.read_day(DAY) if r["kind"] == "hour"]
    assert (rows[0]["wind_dir_deg"], rows[0]["wind_dir"]) == (275, "W")

    # An archived day is never rewritten.
    assert await writer.async_catch_up(date(2026, 11, 24)) == []
