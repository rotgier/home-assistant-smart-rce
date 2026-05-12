"""Tests for weather_table.assemble_rows — dedupe, source labels, edge cases."""

from datetime import date, datetime

from custom_components.smart_rce.domain.weather_table import (
    DEDUPE_FIELDS,
    SOURCE_CURRENT,
    SOURCE_FORECAST,
    SOURCE_HISTORY,
    SOURCE_NOWCAST,
    StateSnapshot,
    assemble_rows,
)
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Warsaw")


def _ts(h: int, m: int = 0, day: int = 12) -> datetime:
    return datetime(2026, 5, day, h, m, 0, tzinfo=TZ)


def _history(
    items: list[tuple[str, datetime, str | None]],
) -> dict[str, list[StateSnapshot]]:
    """Build history_per_sensor dict from (entity_id, ts, value) triples."""
    out: dict[str, list[StateSnapshot]] = {}
    for entity_id, ts, val in items:
        out.setdefault(entity_id, []).append(StateSnapshot(ts, val))
    for snaps in out.values():
        snaps.sort(key=lambda s: s.timestamp)
    return out


def test_empty_history_no_today_returns_empty():
    rows = assemble_rows(
        history_per_sensor={},
        target_date=date(2026, 5, 12),
        now=_ts(12, 30),
        current_obs=None,
        forecast_hours=[],
        nowcast_items=[],
        tz=TZ,
    )
    assert rows == []


def test_single_history_change_emits_row():
    history = _history(
        [
            ("sensor.wetteronline_condition_custom", _ts(10, 0), "cloudy"),
            ("sensor.wetteronline_precipitation_probability", _ts(10, 0), "20"),
        ]
    )
    rows = assemble_rows(
        history_per_sensor=history,
        target_date=date(2026, 5, 12),
        now=_ts(15, 0, day=13),  # tomorrow → no current/nowcast/forecast
        current_obs=None,
        forecast_hours=[],
        nowcast_items=[],
        tz=TZ,
    )
    assert len(rows) == 1
    assert rows[0]["source"] == SOURCE_HISTORY
    assert rows[0]["condition_custom"] == "cloudy"
    assert rows[0]["precipitation_probability"] == 20.0
    assert rows[0]["multiplier"] == 1.0  # prob < 30 → no rain shortcut


def test_consecutive_identical_rows_deduplicated():
    history = _history(
        [
            ("sensor.wetteronline_condition_custom", _ts(10, 0), "cloudy"),
            ("sensor.wetteronline_precipitation_probability", _ts(10, 5), "20"),
            # Different timestamp but same condition+prob — should collapse
            ("sensor.wetteronline_precipitation_probability", _ts(10, 10), "20"),
        ]
    )
    rows = assemble_rows(
        history_per_sensor=history,
        target_date=date(2026, 5, 12),
        now=_ts(15, 0, day=13),
        current_obs=None,
        forecast_hours=[],
        nowcast_items=[],
        tz=TZ,
    )
    # Three timestamps but identical 8-field snapshot after 10:05 → dedupe to 2:
    # row at 10:00 (cloudy + None prob), row at 10:05 (cloudy + 20)
    assert len(rows) == 2
    assert rows[0]["precipitation_probability"] is None
    assert rows[1]["precipitation_probability"] == 20.0


def test_history_and_nowcast_merge_today():
    """target_date == today: history + nowcast emit rows; both kept when fields differ."""
    history = _history(
        [
            ("sensor.wetteronline_condition_custom", _ts(10, 0), "cloudy"),
        ]
    )
    nowcast = [
        {
            "date": _ts(13, 0).isoformat(),
            "condition_custom": "rainy",
            "precipitation_probability": 80,
        }
    ]
    now = _ts(12, 30)
    rows = assemble_rows(
        history_per_sensor=history,
        target_date=date(2026, 5, 12),
        now=now,
        current_obs=None,
        forecast_hours=[],
        nowcast_items=nowcast,
        tz=TZ,
    )
    sources = [r["source"] for r in rows]
    assert SOURCE_HISTORY in sources
    assert SOURCE_NOWCAST in sources
    # Sorted by datetime: 10:00 history first, 13:00 nowcast second
    assert rows[0]["time"] == "10:00"
    assert rows[-1]["time"] == "13:00"


def test_synthesized_current_hour_only_today():
    current_obs = {
        "condition_custom": "pouring",
        "precipitation_probability": 90,
        "precipitation_amount_mm_max": 1.0,
        "precipitation_duration_min_max": 60,
    }
    now = _ts(12, 30)
    rows = assemble_rows(
        history_per_sensor={},
        target_date=date(2026, 5, 12),
        now=now,
        current_obs=current_obs,
        forecast_hours=[],
        nowcast_items=[],
        tz=TZ,
    )
    assert len(rows) == 1
    assert rows[0]["source"] == SOURCE_CURRENT
    assert rows[0]["time"] == "12:00"  # rounded to hour start
    assert rows[0]["condition_custom"] == "pouring"
    # Full hour heavy rain → multiplier ≈ 0.55 ((1 * (0.4 + 0.5*0.2) * 1)=0.5, mul=0.5)
    assert rows[0]["multiplier"] < 1.0


def test_forecast_skips_current_hour_and_past():
    """Forecast contributes only hours strictly after now's clock hour."""
    now = _ts(12, 30)
    forecast = [
        # current hour — skip
        {"datetime": _ts(12, 0).isoformat(), "condition_custom": "cloudy"},
        # next hour — keep
        {
            "datetime": _ts(13, 0).isoformat(),
            "condition_custom": "rainy",
            "precipitation_probability": 60,
        },
    ]
    rows = assemble_rows(
        history_per_sensor={},
        target_date=date(2026, 5, 12),
        now=now,
        current_obs=None,
        forecast_hours=forecast,
        nowcast_items=[],
        tz=TZ,
    )
    assert len(rows) == 1
    assert rows[0]["time"] == "13:00"
    assert rows[0]["source"] == SOURCE_FORECAST


def test_past_date_ignores_current_and_future_sources():
    """When target_date != today, current/nowcast/forecast are dropped."""
    history = _history(
        [("sensor.wetteronline_condition_custom", _ts(8, 0, day=11), "sunny")]
    )
    rows = assemble_rows(
        history_per_sensor=history,
        target_date=date(2026, 5, 11),
        now=_ts(12, 30),
        current_obs={"condition_custom": "rainy", "precipitation_probability": 90},
        forecast_hours=[
            {"datetime": _ts(15, 0).isoformat(), "condition_custom": "cloudy"}
        ],
        nowcast_items=[{"date": _ts(13, 0).isoformat(), "condition_custom": "rainy"}],
        tz=TZ,
    )
    assert len(rows) == 1
    assert rows[0]["source"] == SOURCE_HISTORY
    assert rows[0]["condition_custom"] == "sunny"


def test_unknown_state_parsed_as_none():
    history = _history(
        [
            (
                "sensor.wetteronline_precipitation_probability",
                _ts(10, 0),
                "unknown",
            ),
            (
                "sensor.wetteronline_precipitation_probability",
                _ts(10, 5),
                "30",
            ),
        ]
    )
    rows = assemble_rows(
        history_per_sensor=history,
        target_date=date(2026, 5, 12),
        now=_ts(15, 0, day=13),
        current_obs=None,
        forecast_hours=[],
        nowcast_items=[],
        tz=TZ,
    )
    assert rows[0]["precipitation_probability"] is None
    assert rows[1]["precipitation_probability"] == 30.0


def test_dedupe_fields_contains_all_eight_sensor_fields():
    """Ensure dedupe comparison includes every sensor-derived field."""
    expected = {
        "condition_custom",
        "precipitation_probability",
        "precipitation_amount_mm_min",
        "precipitation_amount_mm_max",
        "precipitation_duration_min_min",
        "precipitation_duration_min_max",
        "convection_probability",
        "visibility_meter",
    }
    assert set(DEDUPE_FIELDS) == expected


def test_history_timestamps_outside_target_date_excluded():
    """State changes on different days don't generate rows for target_date."""
    history = _history(
        [
            ("sensor.wetteronline_condition_custom", _ts(10, 0, day=11), "sunny"),
            ("sensor.wetteronline_condition_custom", _ts(10, 0, day=12), "cloudy"),
        ]
    )
    rows = assemble_rows(
        history_per_sensor=history,
        target_date=date(2026, 5, 12),
        now=_ts(15, 0, day=13),
        current_obs=None,
        forecast_hours=[],
        nowcast_items=[],
        tz=TZ,
    )
    assert len(rows) == 1
    assert rows[0]["condition_custom"] == "cloudy"
