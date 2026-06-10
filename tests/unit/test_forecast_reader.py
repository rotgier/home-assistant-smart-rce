"""Unit tests for the garden forecast reader (forecast_hourly → ForecastSlot)."""

from datetime import UTC, datetime, timedelta

from custom_components.smart_rce.garden.infrastructure.forecast_reader import (
    parse_forecast_slots,
)


def test_hourly_without_nowcast_becomes_60min_slot() -> None:
    raw = [
        {
            "datetime": "2026-06-09T12:00:00+00:00",
            "precipitation_probability": 40,
            "nowcast_15min": [],
        }
    ]
    slots = parse_forecast_slots(raw)

    assert len(slots) == 1
    assert slots[0].start == datetime(2026, 6, 9, 12, tzinfo=UTC)
    assert slots[0].rain_prob == 40
    assert slots[0].duration == timedelta(minutes=60)


def test_nowcast_expands_to_15min_slots() -> None:
    raw = [
        {
            "datetime": "2026-06-09T12:00:00+00:00",
            "precipitation_probability": 40,
            "nowcast_15min": [
                {"date": "2026-06-09T12:00:00+00:00", "precipitation_probability": 10},
                {"date": "2026-06-09T12:15:00+00:00", "precipitation_probability": 60},
            ],
        }
    ]
    slots = parse_forecast_slots(raw)

    assert len(slots) == 2
    assert all(s.duration == timedelta(minutes=15) for s in slots)
    assert [s.rain_prob for s in slots] == [10, 60]
    assert slots[1].start == datetime(2026, 6, 9, 12, 15, tzinfo=UTC)


def test_missing_probability_defaults_to_zero() -> None:
    raw = [{"datetime": "2026-06-09T12:00:00+00:00"}]
    slots = parse_forecast_slots(raw)

    assert slots[0].rain_prob == 0


def test_bad_or_missing_date_skipped() -> None:
    raw = [
        {"precipitation_probability": 50},  # no datetime
        {"datetime": "not-a-date", "precipitation_probability": 50},
    ]

    assert parse_forecast_slots(raw) == []


def test_none_empty_and_non_dict_entries() -> None:
    assert parse_forecast_slots(None) == []
    assert parse_forecast_slots([]) == []
    assert parse_forecast_slots(["x", 5]) == []
