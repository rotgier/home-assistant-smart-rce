"""Unit tests for the garden non_work reader (hass read + pure parse)."""

from datetime import time
from unittest.mock import MagicMock

from custom_components.smart_rce.garden.domain.non_work import NonWorkHours
from custom_components.smart_rce.garden.infrastructure.non_work_reader import (
    parse_non_work_state,
    read_non_work_hours,
)


def test_parses_nominal_window() -> None:
    assert parse_non_work_state("08:35pm - 10:05am") == NonWorkHours(
        start=time(20, 35), end=time(10, 5)
    )


def test_window_crosses_midnight_end_before_start() -> None:
    result = parse_non_work_state("08:35pm - 10:05am")
    assert result is not None
    assert result.end < result.start  # 10:05 < 20:35


def test_am_pm_boundaries() -> None:
    assert parse_non_work_state("12:00am - 12:00pm") == NonWorkHours(
        start=time(0, 0), end=time(12, 0)
    )


def test_tolerates_whitespace_and_case() -> None:
    assert parse_non_work_state("  8:35PM-10:05AM ") == NonWorkHours(
        start=time(20, 35), end=time(10, 5)
    )


def test_unavailable_and_garbage_return_none() -> None:
    for bad in [None, "unknown", "unavailable", "", "20:35", "nonsense", "a - b"]:
        assert parse_non_work_state(bad) is None


def test_read_returns_parsed_value_from_hass_state() -> None:
    hass = MagicMock()
    hass.states.get.return_value = MagicMock(state="08:35pm - 10:05am")

    assert read_non_work_hours(hass, "sensor.x") == NonWorkHours(
        start=time(20, 35), end=time(10, 5)
    )


def test_read_returns_none_when_entity_missing() -> None:
    hass = MagicMock()
    hass.states.get.return_value = None

    assert read_non_work_hours(hass, "sensor.x") is None
