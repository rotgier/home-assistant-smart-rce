"""Tests for the standalone planner field sensors (presentation specs).

Covers the `_PLANNER_FIELDS` value extractors and that they mirror the
corresponding `PlannerDecision` fields — entity wiring (hass/add_entities) is
covered by the platform setup, here we pin the field-to-value mapping.
"""

from datetime import UTC, datetime

from custom_components.smart_rce.garden.domain.forecast_window import WindowBound
from custom_components.smart_rce.garden.domain.mowing_planner import (
    PlannerDecision,
    StartStrategy,
)
from custom_components.smart_rce.garden.sensor_entities import _PLANNER_FIELDS

WINDOW_START = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 6, 9, 18, 0, tzinfo=UTC)
OPT_START = datetime(2026, 6, 9, 12, 30, tzinfo=UTC)

DECISION = PlannerDecision(
    should_start=True,
    window_start=WINDOW_START,
    window_end=WINDOW_END,
    opt_start=OPT_START,
    window_bound=WindowBound.RAIN,
    strategy=StartStrategy.GO,
    needed_min=120,
    window_min=360,
    time_to_drain_min=116,
    time_to_finish_min=2,
    battery=90,
    progress=98,
    at_dock=True,
)

EXPECTED = {
    "mowing_window_start": WINDOW_START,
    "mowing_window_end": WINDOW_END,
    "mowing_opt_start": OPT_START,
    "mowing_window_bound": "rain",
    "mowing_window_min": 360,
    "mowing_needed_min": 120,
    "mowing_time_to_drain": 116,
    "mowing_time_to_finish": 2,
}


def test_planner_fields_extract_expected_values() -> None:
    actual = {field.key: field.value(DECISION) for field in _PLANNER_FIELDS}
    assert actual == EXPECTED


def test_planner_fields_keys_are_unique() -> None:
    keys = [field.key for field in _PLANNER_FIELDS]
    assert len(keys) == len(set(keys))


def test_planner_fields_skip_native_device_sensors() -> None:
    # battery/progress/at_dock have native mammotion sensors; strategy is the
    # parent sensor's own state — none should be duplicated here.
    keys = {field.key for field in _PLANNER_FIELDS}
    assert not (keys & {"battery", "progress", "at_dock", "strategy"})
