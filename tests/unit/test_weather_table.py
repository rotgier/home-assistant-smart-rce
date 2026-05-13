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


def test_synthesized_current_uses_fetched_at_timestamp():
    """Current row datetime should reflect the live-snapshot moment.

    fetched_at is "when wo-cloud was last polled" — that is the time at
    which the live point-in-time values (Prob, Cond) are valid. Using it
    as the row timestamp positions the row chronologically alongside
    history rows from the same hour, instead of pinning it to the hour
    boundary (which would imply the values held since :00).
    """
    current_obs = {
        "condition_custom": "pouring",
        "precipitation_probability": 90,
        "precipitation_amount_mm_max": 1.0,
        "precipitation_duration_min_max": 60,
        "fetched_at": "2026-05-12T12:15:30+02:00",
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
    assert rows[0]["time"] == "12:15"  # from fetched_at, not hour boundary
    assert rows[0]["condition_custom"] == "pouring"
    assert rows[0]["multiplier"] < 1.0


def test_synthesized_current_falls_back_to_hour_when_fetched_at_missing():
    """Missing fetched_at → use the current clock hour as the row anchor."""
    current_obs = {
        "condition_custom": "cloudy",
        "precipitation_probability": 20,
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
    assert rows[0]["time"] == "12:00"


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


def test_future_date_includes_forecast_rows():
    """Tomorrow target_date should surface forecast rows from the hourly list.

    wo-cloud hourly forecast extends ~49 hours, so forecast for tomorrow
    is available — the table view for date+1 should show those rows even
    though there's no recorder history yet for that day.
    """
    now = _ts(12, 30)
    tomorrow = date(2026, 5, 13)
    forecast = [
        # today's later hour — wrong date, skip
        {"datetime": _ts(20, 0).isoformat(), "condition_custom": "cloudy"},
        # tomorrow morning — keep
        {
            "datetime": _ts(8, 0, day=13).isoformat(),
            "condition_custom": "partlycloudy",
            "precipitation_probability": 10,
        },
        # tomorrow afternoon — keep
        {
            "datetime": _ts(15, 0, day=13).isoformat(),
            "condition_custom": "sunny",
            "precipitation_probability": 0,
        },
    ]
    rows = assemble_rows(
        history_per_sensor={},
        target_date=tomorrow,
        now=now,
        current_obs={"condition_custom": "rainy"},  # ignored — not today
        forecast_hours=forecast,
        nowcast_items=[],
        tz=TZ,
    )
    assert len(rows) == 2
    assert all(r["source"] == SOURCE_FORECAST for r in rows)
    assert rows[0]["time"] == "08:00"
    assert rows[1]["time"] == "15:00"


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


def test_subsecond_jitter_clustered_to_single_row():
    """8 sensors with sub-second offsets per coordinator tick → 1 row, not 8.

    Without clustering, each near-identical timestamp would yield its own
    row showing a "staircase" of intermediate states as sensor-after-
    sensor updates. With clustering the anchor is the cluster's latest
    timestamp, so state-at-anchor sees every sensor's new value.
    """
    base = datetime(2026, 5, 12, 10, 11, 0, tzinfo=TZ)
    items = []
    # Two clusters: ~10:11 (8 sensors fire within ~0.5s) and ~10:16 (the
    # next coordinator tick, also 8 sensors with sub-second jitter).
    for i, sensor_id in enumerate(
        [
            "sensor.wetteronline_condition_custom",
            "sensor.wetteronline_precipitation_probability",
            "sensor.wetteronline_precipitation_amount_min",
            "sensor.wetteronline_precipitation_amount_max",
            "sensor.wetteronline_precipitation_duration_min",
            "sensor.wetteronline_precipitation_duration_max",
            "sensor.wetteronline_convection_probability",
            "sensor.wetteronline_visibility",
        ]
    ):
        # Cluster 1 at 10:11
        items.append(
            (
                sensor_id,
                base.replace(microsecond=i * 50_000),
                "cloudy" if sensor_id.endswith("condition_custom") else "10",
            )
        )
        # Cluster 2 at 10:16
        five_min = base.replace(minute=16, microsecond=i * 50_000)
        items.append(
            (
                sensor_id,
                five_min,
                "cloudy" if sensor_id.endswith("condition_custom") else "20",
            )
        )
    history = _history(items)
    rows = assemble_rows(
        history_per_sensor=history,
        target_date=date(2026, 5, 12),
        now=_ts(15, 0, day=13),
        current_obs=None,
        forecast_hours=[],
        nowcast_items=[],
        tz=TZ,
    )
    # Expect exactly 2 rows (one per cluster), not 16 staircase rows.
    assert len(rows) == 2
    assert rows[0]["precipitation_probability"] == 10.0
    assert rows[1]["precipitation_probability"] == 20.0


def test_nowcast_deduped_against_previous_current_when_identical():
    """Drop nowcast when 8 fields match the preceding current row.

    Sub-hour refinement is noise when the previous row already carries the
    same values.
    """
    fetched_at = _ts(12, 30)
    nowcast_same = {
        "date": _ts(12, 45).isoformat(),
        "condition_custom": "cloudy",
        "precipitation_probability": 40,
    }
    nowcast_different = {
        "date": _ts(13, 0).isoformat(),
        "condition_custom": "pouring-light",
        "precipitation_probability": 80,
    }
    current_obs = {
        "condition_custom": "cloudy",
        "precipitation_probability": 40,
        "fetched_at": fetched_at.isoformat(),
    }
    rows = assemble_rows(
        history_per_sensor={},
        target_date=date(2026, 5, 12),
        now=fetched_at,
        current_obs=current_obs,
        forecast_hours=[],
        nowcast_items=[nowcast_same, nowcast_different],
        tz=TZ,
    )
    sources_with_times = [(r["source"], r["time"]) for r in rows]
    # current at 12:30 kept; nowcast 12:45 (same fields as current) dropped;
    # nowcast 13:00 (different) kept.
    assert (SOURCE_CURRENT, "12:30") in sources_with_times
    assert (SOURCE_NOWCAST, "12:45") not in sources_with_times
    assert (SOURCE_NOWCAST, "13:00") in sources_with_times


def test_nowcast_not_deduped_against_previous_history():
    """History row should NOT silently shadow a future-looking nowcast point."""
    same_moment = _ts(13, 0)
    history = _history(
        [
            ("sensor.wetteronline_condition_custom", _ts(12, 50), "cloudy"),
            (
                "sensor.wetteronline_precipitation_probability",
                _ts(12, 50),
                "40",
            ),
        ]
    )
    nowcast = [
        {
            "date": same_moment.isoformat(),
            "condition_custom": "cloudy",
            "precipitation_probability": 40,
        }
    ]
    rows = assemble_rows(
        history_per_sensor=history,
        target_date=date(2026, 5, 12),
        now=_ts(12, 55),
        current_obs=None,
        forecast_hours=[],
        nowcast_items=nowcast,
        tz=TZ,
    )
    sources = [r["source"] for r in rows]
    assert SOURCE_HISTORY in sources
    assert SOURCE_NOWCAST in sources


def test_history_dropped_when_identical_to_current_at_same_time():
    """History row matching current on every input field → drop history.

    Aligned coordinator may emit a history snapshot at the same minute as
    the synthesized-current fetched_at. Two visually identical rows back-
    to-back add noise without information — keep the more meaningful
    `current` (carries the live-snapshot semantic + nowcast items).
    """
    same_moment = _ts(13, 30)
    history = _history(
        [
            ("sensor.wetteronline_condition_custom", same_moment, "cloudy"),
            (
                "sensor.wetteronline_precipitation_probability",
                same_moment,
                "40",
            ),
        ]
    )
    current_obs = {
        "condition_custom": "cloudy",
        "precipitation_probability": 40,
        # Identical to the history row's 8 fields:
        "fetched_at": same_moment.isoformat(),
    }
    rows = assemble_rows(
        history_per_sensor=history,
        target_date=date(2026, 5, 12),
        now=same_moment,
        current_obs=current_obs,
        forecast_hours=[],
        nowcast_items=[],
        tz=TZ,
    )
    sources = [r["source"] for r in rows]
    assert SOURCE_CURRENT in sources
    assert SOURCE_HISTORY not in sources


def test_history_kept_when_same_fields_but_different_time_than_current():
    """History rain-start at 12:25 must NOT be shadowed by current at 12:30.

    Same fields (rain just started) but different HH:MM — the history row
    documents the transition moment ('rain started at 12:25'); dropping
    it would lose that information.
    """
    history = _history(
        [
            ("sensor.wetteronline_condition_custom", _ts(12, 25), "pouring-light"),
            (
                "sensor.wetteronline_precipitation_probability",
                _ts(12, 25),
                "80",
            ),
        ]
    )
    current_obs = {
        "condition_custom": "pouring-light",
        "precipitation_probability": 80,
        # Same fields, 5 min later:
        "fetched_at": _ts(12, 30).isoformat(),
    }
    rows = assemble_rows(
        history_per_sensor=history,
        target_date=date(2026, 5, 12),
        now=_ts(12, 30),
        current_obs=current_obs,
        forecast_hours=[],
        nowcast_items=[],
        tz=TZ,
    )
    times_sources = [(r["time"], r["source"]) for r in rows]
    assert ("12:25", SOURCE_HISTORY) in times_sources
    assert ("12:30", SOURCE_CURRENT) in times_sources


def test_history_kept_when_differs_from_current_at_same_time():
    """History row matching current's timestamp but different fields → keep both.

    Same minute but different precipitation_probability values means a
    real state change just before the synthesized snapshot — both rows
    carry information.
    """
    same_moment = _ts(13, 30)
    history = _history(
        [
            ("sensor.wetteronline_condition_custom", same_moment, "cloudy"),
            (
                "sensor.wetteronline_precipitation_probability",
                same_moment,
                "20",  # ← different from current's 40
            ),
        ]
    )
    current_obs = {
        "condition_custom": "cloudy",
        "precipitation_probability": 40,
        "fetched_at": same_moment.isoformat(),
    }
    rows = assemble_rows(
        history_per_sensor=history,
        target_date=date(2026, 5, 12),
        now=same_moment,
        current_obs=current_obs,
        forecast_hours=[],
        nowcast_items=[],
        tz=TZ,
    )
    sources = [r["source"] for r in rows]
    assert SOURCE_HISTORY in sources
    assert SOURCE_CURRENT in sources


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
