"""Parsing helpers of the scoreboard's HA sources."""

from __future__ import annotations

from custom_components.smart_rce.infrastructure.forecast_score_sources import (
    _plan_hours,
)


def test_plan_hours_are_hours_of_the_day():
    """The plan sensor lists "08:00"; D3 compares hours (8), not minutes (480)."""
    assert _plan_hours({"hours": ["08:00", "09:00", "10:00", "x"]}) == (8, 9, 10)
    assert _plan_hours({}) == ()
