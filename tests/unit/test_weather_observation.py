"""Observed weather archive: raw series and time-weighted hourly rows (24.11.2026)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from zoneinfo import ZoneInfo

from custom_components.smart_rce.domain.weather_observation import (
    OUTSIDE_TEMP,
    WIND_BEARING,
    WIND_SPEED,
    WO_PRECIPITATION_TYPE,
    ObservedSeries,
    ObservedWeatherDay,
    SeriesPoint,
)
import pytest

TZ = ZoneInfo("Europe/Warsaw")
DAY = date(2026, 11, 24)


def _at(h: int, m: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


def _series(start_value, *changes, entity="sensor.x", unit=None) -> ObservedSeries:
    return ObservedSeries(
        entity, unit, start_value, tuple(SeriesPoint(at, v) for at, v in changes)
    )


def _day(**series: ObservedSeries) -> ObservedWeatherDay:
    return ObservedWeatherDay(DAY, TZ, series)


def _hour(day: ObservedWeatherDay, h: int):
    return next(row for row in day.hours() if row.start == _at(h))


class TestWind:
    def test_north_is_averaged_as_a_vector_not_as_numbers(self):
        # 350° and 10° for half an hour each: north, not 180°.
        day = _day(
            wind_speed=_series(20.0),
            wind_bearing=_series(350.0, (_at(5, 30), 10.0)),
        )
        wind = _hour(day, 5).wind
        assert wind.direction_deg == 0
        assert wind.direction == "N"
        assert wind.steadiness == pytest.approx(0.98, abs=0.01)  # cos 10°

    def test_the_faster_wind_weighs_more(self):
        # 10 km/h from the east for 30 min, 30 km/h from the south for 30 min.
        day = _day(
            wind_speed=_series(10.0, (_at(5, 30), 30.0)),
            wind_bearing=_series(90.0, (_at(5, 30), 180.0)),
        )
        wind = _hour(day, 5).wind
        assert wind.mean_kmh == 20.0
        assert wind.max_kmh == 30.0
        assert wind.direction_deg == 162  # atan2(10, -30): pulled towards south
        assert wind.direction == "S"

    def test_one_direction_all_hour_is_fully_steady(self):
        day = _day(wind_speed=_series(15.0), wind_bearing=_series(270.0))
        wind = _hour(day, 3).wind
        assert (wind.direction_deg, wind.direction, wind.steadiness) == (270, "W", 1.0)
        assert wind.cover_min == 60

    def test_calm_has_no_direction(self):
        day = _day(wind_speed=_series(0.0), wind_bearing=_series(270.0))
        wind = _hour(day, 3).wind
        assert wind.mean_kmh == 0.0
        assert (wind.direction_deg, wind.direction, wind.steadiness) == (
            None,
            None,
            None,
        )

    def test_opposite_winds_cancel_out(self):
        day = _day(
            wind_speed=_series(10.0),
            wind_bearing=_series(90.0, (_at(5, 30), 270.0)),
        )
        wind = _hour(day, 5).wind
        assert wind.steadiness == 0.0
        assert wind.direction_deg is None

    def test_an_outage_does_not_carry_the_value(self):
        day = _day(
            wind_speed=_series(10.0, (_at(5, 15), None), (_at(5, 45), 10.0)),
            wind_bearing=_series(270.0),
        )
        assert _hour(day, 5).wind.cover_min == 30
        assert _hour(day, 4).wind.cover_min == 60

    def test_no_data_leaves_the_hour_empty(self):
        wind = _hour(_day(wind_bearing=_series(270.0)), 5).wind
        assert wind.mean_kmh is None
        assert wind.cover_min == 0


class TestMeasures:
    def test_temperature_is_time_weighted(self):
        # -1 for 45 minutes, +3 for 15: mean 0, min -1, max 3.
        day = _day(outside_temp=_series(-1.0, (_at(6, 45), 3.0)))
        temp = _hour(day, 6).measures[OUTSIDE_TEMP]
        assert (temp.mean, temp.min, temp.max, temp.cover_min) == (0.0, -1.0, 3.0, 60)
        record = _hour(day, 6).to_record()
        assert record["temp_c_mean"] == 0.0
        assert record["temp_c_cover_min"] == 60

    def test_a_text_series_is_kept_raw_and_not_averaged(self):
        day = _day(wo_precipitation_type=_series("rain", (_at(8), "snow")))
        assert WO_PRECIPITATION_TYPE not in _hour(day, 8).measures
        raw = next(r for r in day.to_records() if r["kind"] == "raw")
        assert raw["in_force_at_start"] == "rain"
        assert raw["points"] == [[_at(8).isoformat(), "snow"]]


class TestDay:
    def test_a_day_has_its_real_hours(self):
        assert len(_day().hours()) == 24
        autumn = ObservedWeatherDay(date(2026, 10, 25), TZ, {})
        assert len(autumn.hours()) == 25  # 02:00 twice (CEST, then CET)
        spring = ObservedWeatherDay(date(2027, 3, 28), TZ, {})
        assert len(spring.hours()) == 23

    def test_the_state_at_midnight_is_carried_in_not_a_reading(self):
        midnight = _at(0)
        rows = [
            (midnight - timedelta(hours=3), 12.0),
            (_at(1, 10), 14.0),
            (_at(2, 0), None),
        ]
        series = ObservedSeries.of(
            "sensor.wetteronline_wind_speed", "km/h", rows, midnight
        )
        assert series.in_force_at_start == 12.0
        assert [p.value for p in series.points] == [14.0, None]

    def test_records_round_trip_and_describe_themselves(self):
        day = _day(
            wind_speed=_series(15.0, (_at(9, 20), 20.0), unit="km/h"),
            wind_bearing=_series(265.0, unit="°"),
            outside_temp=_series(-0.3, (_at(9, 1), -0.1), unit="°C"),
        )
        records = json.loads(json.dumps(day.to_records()))
        assert records[0]["kind"] == "meta"
        assert records[0]["version"] == ObservedWeatherDay.VERSION
        assert sum(r["kind"] == "raw" for r in records) == 3
        assert sum(r["kind"] == "hour" for r in records) == 24
        back = ObservedWeatherDay.from_records(DAY, TZ, records)
        assert back.series[WIND_SPEED] == day.series[WIND_SPEED]
        assert back.hours() == day.hours()
        assert back.series[WIND_BEARING].unit == "°"
