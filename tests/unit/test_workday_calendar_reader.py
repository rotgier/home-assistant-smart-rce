"""Tests for `WorkdayCalendarReader.fetch_workdays`.

Mocks HA's `calendar.get_events` service call: covers happy-path date
parsing, missing entity (empty response), and service failure (defensive
empty set instead of crashing).
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

from custom_components.smart_rce.const import WORKDAY_CALENDAR_ENTITY
from custom_components.smart_rce.infrastructure.workday_calendar_reader import (
    WorkdayCalendarReader,
)
import pytest


def _hass_with_calendar_response(events: list[dict]) -> MagicMock:
    """Build a fake HASS whose `services.async_call` returns a calendar response."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(
        return_value={WORKDAY_CALENDAR_ENTITY: {"events": events}}
    )
    return hass


@pytest.mark.asyncio
async def test_parses_date_only_event_starts() -> None:
    """Workday all-day events surface as `start: 'YYYY-MM-DD'` strings."""
    hass = _hass_with_calendar_response(
        [
            {"start": "2026-05-11", "end": "2026-05-12", "summary": "Workday"},
            {"start": "2026-05-08", "end": "2026-05-09", "summary": "Workday"},
            {"start": "2026-05-07", "end": "2026-05-08", "summary": "Workday"},
        ]
    )
    reader = WorkdayCalendarReader(hass)
    workdays = await reader.fetch_workdays(date(2026, 5, 12))
    assert workdays == {
        date(2026, 5, 11),
        date(2026, 5, 8),
        date(2026, 5, 7),
    }


@pytest.mark.asyncio
async def test_passes_correct_window_to_service() -> None:
    """Service call gets entity_id + ISO-formatted window edges."""
    hass = _hass_with_calendar_response([])
    reader = WorkdayCalendarReader(hass)
    await reader.fetch_workdays(date(2026, 5, 12), lookback_days=14)
    args, kwargs = hass.services.async_call.call_args
    assert args[0] == "calendar"
    assert args[1] == "get_events"
    payload = args[2]
    assert payload["entity_id"] == WORKDAY_CALENDAR_ENTITY
    assert payload["start_date_time"].startswith("2026-04-28T00:00:00")
    assert payload["end_date_time"].startswith("2026-05-12T00:00:00")
    assert kwargs.get("blocking") is True
    assert kwargs.get("return_response") is True


@pytest.mark.asyncio
async def test_empty_response_returns_empty_set() -> None:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)
    reader = WorkdayCalendarReader(hass)
    assert await reader.fetch_workdays(date(2026, 5, 12)) == set()


@pytest.mark.asyncio
async def test_missing_entity_in_response_returns_empty_set() -> None:
    """Response present but doesn't contain our entity → empty set."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(
        return_value={"calendar.other": {"events": []}}
    )
    reader = WorkdayCalendarReader(hass)
    assert await reader.fetch_workdays(date(2026, 5, 12)) == set()


@pytest.mark.asyncio
async def test_service_call_failure_returns_empty_set() -> None:
    """Defensive: service errors → empty set + logged exception, no crash."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))
    reader = WorkdayCalendarReader(hass)
    assert await reader.fetch_workdays(date(2026, 5, 12)) == set()


@pytest.mark.asyncio
async def test_parses_datetime_event_starts() -> None:
    """Some HA versions return datetime strings instead of date-only.

    Uses noon UTC so the tz-conversion done inside the reader (to
    `dt_util.DEFAULT_TIME_ZONE`) stays on the same calendar date in any
    reasonable test/runtime tz.
    """
    hass = _hass_with_calendar_response(
        [
            {"start": "2026-05-11T12:00:00+00:00", "end": "2026-05-12T12:00:00+00:00"},
            {"start": "2026-05-08T12:00:00+00:00", "end": "2026-05-09T12:00:00+00:00"},
        ]
    )
    reader = WorkdayCalendarReader(hass)
    workdays = await reader.fetch_workdays(date(2026, 5, 12))
    assert workdays == {date(2026, 5, 11), date(2026, 5, 8)}


@pytest.mark.asyncio
async def test_malformed_event_entries_are_skipped() -> None:
    """Garbage entries (missing start, wrong type) are dropped silently."""
    hass = _hass_with_calendar_response(
        [
            {"start": "2026-05-11"},
            {"summary": "no start field"},
            "not a dict",
            {"start": None},
            {"start": "not-a-date"},
            {"start": "2026-05-08"},
        ]
    )
    reader = WorkdayCalendarReader(hass)
    workdays = await reader.fetch_workdays(date(2026, 5, 12))
    assert workdays == {date(2026, 5, 11), date(2026, 5, 8)}
