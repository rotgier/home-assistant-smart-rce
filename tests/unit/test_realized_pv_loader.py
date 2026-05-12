"""Tests for `RealizedPvLoader.fetch_for_dates` batched recorder query.

Mocks the recorder so we don't need a live HA. Verifies date bucketing,
window selection (single query spans min..max date), and that only the
last 5-min slot in each cycle (xx:25 / xx:55) is captured.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.smart_rce.infrastructure.pv_forecast.realized_pv_loader import (
    RealizedPvLoader,
)
import pytest

# Test machine TZ is UTC (see test_workday_calendar_reader). Use UTC slots
# so the .astimezone(local) call inside the loader is a no-op.
_TZ = timezone.utc


def _slot(ts: datetime, value: float) -> dict:
    """Build one recorder stats slot in the shape `statistics_during_period` emits."""
    return {"start": ts.replace(tzinfo=UTC).timestamp(), "state": value}


def _hass_with_stats(slots_by_entity: dict[str, list[dict]]) -> MagicMock:
    """Build a fake HASS whose recorder executor returns `slots_by_entity`."""
    hass = MagicMock()

    async def _run_executor(fn, *args, **_kwargs):
        # statistics_during_period(hass, start, end, entity_ids, ...) — last
        # positional that matters here is the entity_ids set; we just
        # return the configured slots irrespective of args (mocking
        # behavior, not the recorder's filtering).
        return slots_by_entity

    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(side_effect=_run_executor)
    return hass, instance


@pytest.mark.asyncio
async def test_fetch_for_dates_buckets_per_date() -> None:
    """Each date gets its own bucket dict; slots are routed by ts.date()."""
    day1 = date(2026, 5, 10)
    day2 = date(2026, 5, 11)
    slots = [
        # day1
        _slot(datetime.combine(day1, time(7, 25), tzinfo=_TZ), 0.5),
        _slot(datetime.combine(day1, time(7, 55), tzinfo=_TZ), 0.7),
        _slot(datetime.combine(day1, time(8, 25), tzinfo=_TZ), 0.9),
        # day2
        _slot(datetime.combine(day2, time(9, 25), tzinfo=_TZ), 1.1),
        _slot(datetime.combine(day2, time(9, 55), tzinfo=_TZ), 1.3),
    ]
    hass, instance = _hass_with_stats({"sensor.total_pv_generation_bi_hourly": slots})

    with (
        patch(
            "custom_components.smart_rce.infrastructure.pv_forecast.realized_pv_loader.get_instance",
            return_value=instance,
        ),
        patch(
            "custom_components.smart_rce.infrastructure.pv_forecast.realized_pv_loader.dt_util.DEFAULT_TIME_ZONE",
            _TZ,
        ),
    ):
        loader = RealizedPvLoader(hass)
        result = await loader.fetch_for_dates([day1, day2])

    assert result[day1] == {(7, 0): 0.5, (7, 30): 0.7, (8, 0): 0.9}
    assert result[day2] == {(9, 0): 1.1, (9, 30): 1.3}


@pytest.mark.asyncio
async def test_fetch_for_dates_filters_to_requested_dates_only() -> None:
    """Slots from dates outside `dates` (returned by the LTS span) are dropped."""
    day1 = date(2026, 5, 10)
    day2 = date(2026, 5, 12)  # gap day intentionally not requested
    other = date(2026, 5, 11)
    slots = [
        _slot(datetime.combine(day1, time(7, 25), tzinfo=_TZ), 0.5),
        _slot(datetime.combine(other, time(7, 25), tzinfo=_TZ), 99.0),  # ignored
        _slot(datetime.combine(day2, time(7, 25), tzinfo=_TZ), 0.6),
    ]
    hass, instance = _hass_with_stats({"sensor.total_pv_generation_bi_hourly": slots})

    with (
        patch(
            "custom_components.smart_rce.infrastructure.pv_forecast.realized_pv_loader.get_instance",
            return_value=instance,
        ),
        patch(
            "custom_components.smart_rce.infrastructure.pv_forecast.realized_pv_loader.dt_util.DEFAULT_TIME_ZONE",
            _TZ,
        ),
    ):
        loader = RealizedPvLoader(hass)
        result = await loader.fetch_for_dates([day1, day2])

    assert set(result.keys()) == {day1, day2}
    assert other not in result
    assert result[day1] == {(7, 0): 0.5}
    assert result[day2] == {(7, 0): 0.6}


@pytest.mark.asyncio
async def test_fetch_for_dates_ignores_non_reset_slots() -> None:
    """Only ts.minute in {25, 55} capture full-bucket totals; rest discarded."""
    day = date(2026, 5, 11)
    slots = [
        _slot(datetime.combine(day, time(7, 5), tzinfo=_TZ), 0.1),
        _slot(datetime.combine(day, time(7, 10), tzinfo=_TZ), 0.2),
        _slot(datetime.combine(day, time(7, 25), tzinfo=_TZ), 0.5),  # ← captured
        _slot(datetime.combine(day, time(7, 30), tzinfo=_TZ), 0.0),  # post-reset
        _slot(datetime.combine(day, time(7, 55), tzinfo=_TZ), 0.7),  # ← captured
    ]
    hass, instance = _hass_with_stats({"sensor.total_pv_generation_bi_hourly": slots})

    with (
        patch(
            "custom_components.smart_rce.infrastructure.pv_forecast.realized_pv_loader.get_instance",
            return_value=instance,
        ),
        patch(
            "custom_components.smart_rce.infrastructure.pv_forecast.realized_pv_loader.dt_util.DEFAULT_TIME_ZONE",
            _TZ,
        ),
    ):
        loader = RealizedPvLoader(hass)
        result = await loader.fetch_for_dates([day])

    assert result[day] == {(7, 0): 0.5, (7, 30): 0.7}


@pytest.mark.asyncio
async def test_fetch_for_dates_empty_input_returns_empty_dict() -> None:
    hass = MagicMock()
    loader = RealizedPvLoader(hass)
    assert await loader.fetch_for_dates([]) == {}


@pytest.mark.asyncio
async def test_fetch_today_delegates_to_fetch_for_dates() -> None:
    """Existing `fetch_today` shim still works after the refactor."""
    today = date(2026, 5, 12)
    slots = [_slot(datetime.combine(today, time(10, 25), tzinfo=_TZ), 1.5)]
    hass, instance = _hass_with_stats({"sensor.total_pv_generation_bi_hourly": slots})
    with (
        patch(
            "custom_components.smart_rce.infrastructure.pv_forecast.realized_pv_loader.get_instance",
            return_value=instance,
        ),
        patch(
            "custom_components.smart_rce.infrastructure.pv_forecast.realized_pv_loader.dt_util.DEFAULT_TIME_ZONE",
            _TZ,
        ),
    ):
        loader = RealizedPvLoader(hass)
        result = await loader.fetch_today(today)
    assert result == {(10, 0): 1.5}
