"""Pure-function assembly of the dashboard weather table.

A "row" in the table is a snapshot of the 8 wetteronline sensor fields
plus the computed PV multiplier. Rows come from four sources:

- **history** — past state changes of the wetteronline sensors during
  `target_date`. The union of all state-change timestamps drives row
  emission; at each timestamp each sensor contributes its state-at-or-
  just-before that moment.
- **current** — one row for the current clock hour, synthesized from
  live `current_observations` + cache values (mirrors the synthesized
  hour in `wetteronline.weather.WetterOnlineEntity._async_forecast_hourly`).
  Only when `target_date == today`.
- **nowcast** — 15-min sub-hour slots from wo-cloud nowcast_trend, already
  pre-filtered upstream (wetteronline `_parse_nowcast_items` drops items
  with `dt <= fetched_at`). Only when `target_date == today`.
- **forecast** — hourly forecast items for hours not covered by nowcast,
  read from `weather.get_forecasts(type=hourly)` synthesized list.

After concatenation rows are sorted by datetime and consecutively-
identical rows (bit-exact on the 8 input fields) are dropped — only
state CHANGES surface in the table.

Pure domain. No HA imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, NamedTuple

from .weather_multiplier import compute_multiplier

# Wetteronline sensor entity_ids that drive history-side rows. The
# application layer is responsible for fetching these via the recorder
# and passing the result to `assemble_rows`. Order matches the dedupe
# comparison order below.
WETTERONLINE_SENSORS: tuple[str, ...] = (
    "sensor.wetteronline_condition_custom",
    "sensor.wetteronline_precipitation_probability",
    "sensor.wetteronline_precipitation_amount_mm_min",
    "sensor.wetteronline_precipitation_amount_mm_max",
    "sensor.wetteronline_precipitation_duration_min_min",
    "sensor.wetteronline_precipitation_duration_min_max",
    "sensor.wetteronline_convection_probability",
    "sensor.wetteronline_visibility",
)

# Mapping from sensor entity_id → key used in the output row dict.
SENSOR_FIELD_MAP: Mapping[str, str] = {
    "sensor.wetteronline_condition_custom": "condition_custom",
    "sensor.wetteronline_precipitation_probability": "precipitation_probability",
    "sensor.wetteronline_precipitation_amount_mm_min": "precipitation_amount_mm_min",
    "sensor.wetteronline_precipitation_amount_mm_max": "precipitation_amount_mm_max",
    "sensor.wetteronline_precipitation_duration_min_min": "precipitation_duration_min_min",
    "sensor.wetteronline_precipitation_duration_min_max": "precipitation_duration_min_max",
    "sensor.wetteronline_convection_probability": "convection_probability",
    "sensor.wetteronline_visibility": "visibility_meter",
}

# Fields compared for dedupe (consecutive rows with all of these equal
# collapse into one).
DEDUPE_FIELDS: tuple[str, ...] = tuple(SENSOR_FIELD_MAP.values())

# Sources for the `source` column on each row.
SOURCE_HISTORY = "history"
SOURCE_CURRENT = "current"
SOURCE_NOWCAST = "nowcast"
SOURCE_FORECAST = "forecast"

# Sub-second jitter tolerance when bucketing history timestamps. The 8
# wetteronline sensors all change state at the same coordinator update,
# but each fires its own state_changed event with a slightly different
# sub-second timestamp. Without this clustering each near-identical
# timestamp would yield a separate row showing a "staircase" of values
# as sensor-after-sensor updates. 1s is far above the observed jitter
# (microseconds) and well below the 5-min coordinator cadence.
HISTORY_TIMESTAMP_CLUSTER_TOLERANCE_S = 1.0


class StateSnapshot(NamedTuple):
    """Single state change record (sensor history)."""

    timestamp: datetime
    value: str | None  # raw HA state — None for unknown/unavailable


def assemble_rows(
    history_per_sensor: Mapping[str, list[StateSnapshot]],
    target_date: date,
    now: datetime,
    current_obs: Mapping[str, Any] | None,
    forecast_hours: list[dict[str, Any]],
    nowcast_items: list[dict[str, Any]],
    tz: tzinfo,
) -> list[dict[str, Any]]:
    """Build a deduplicated list of weather table rows for `target_date`.

    `forecast_hours` is the result of `weather.get_forecasts(type=hourly)`
    — each item carries `datetime` (str ISO) and the 8 input fields plus
    optional `nowcast_15min`. `nowcast_items` is the flat nowcast list
    (pre-filtered to strictly-after-fetch slots in wetteronline).

    When `target_date != today`, `current_obs`, `forecast_hours` and
    `nowcast_items` are effectively ignored (no `current`/`nowcast`/
    `forecast` rows emitted).
    """
    candidates: list[dict[str, Any]] = []

    candidates.extend(_history_rows(history_per_sensor, target_date))

    is_today = target_date == now.date()
    if is_today:
        synthesized_current = _current_row(now, tz, current_obs)
        if synthesized_current:
            candidates.append(synthesized_current)
        candidates.extend(_nowcast_rows(nowcast_items, target_date))
        candidates.extend(_forecast_rows(forecast_hours, target_date, now))

    if not candidates:
        return []

    candidates.sort(key=lambda r: r["datetime"])
    return _dedupe_consecutive(candidates)


# --- history (recorder state changes) -----------------------------------


def _history_rows(
    history_per_sensor: Mapping[str, list[StateSnapshot]],
    target_date: date,
) -> list[dict[str, Any]]:
    """Build one row per unique state-change cluster during target_date.

    Sensor timestamps within `HISTORY_TIMESTAMP_CLUSTER_TOLERANCE_S` of
    each other are collapsed to a single anchor (the latest in the
    cluster). At each anchor each sensor contributes its state-at-or-
    just-before that moment — the late anchor guarantees that all
    sensor updates in the cluster are visible (so we don't see partial
    "in-flight" transition rows).
    """
    raw_timestamps = sorted(
        {
            snap.timestamp
            for sensor_id in WETTERONLINE_SENSORS
            for snap in history_per_sensor.get(sensor_id, [])
            if snap.timestamp.date() == target_date
        }
    )
    if not raw_timestamps:
        return []
    anchors = _cluster_timestamps(raw_timestamps, HISTORY_TIMESTAMP_CLUSTER_TOLERANCE_S)
    rows: list[dict[str, Any]] = []
    for ts in anchors:
        row = _empty_row(ts, SOURCE_HISTORY)
        for sensor_id in WETTERONLINE_SENSORS:
            value = _state_at(history_per_sensor.get(sensor_id, []), ts)
            field = SENSOR_FIELD_MAP[sensor_id]
            if field == "condition_custom":
                row[field] = value
            else:
                row[field] = _to_number(value)
        _enrich_with_multiplier(row)
        rows.append(row)
    return rows


def _cluster_timestamps(
    timestamps: list[datetime], tolerance_s: float
) -> list[datetime]:
    """Collapse near-identical timestamps to a single anchor per cluster.

    Returns the LAST timestamp in each cluster as the anchor so that
    `_state_at(anchor)` sees every sensor update that happened in the
    cluster. Without this, sub-second offsets between the 8 sensors'
    state_changed events would produce a 4-8 row staircase showing the
    intermediate "some sensors updated, others not yet" states.

    `timestamps` MUST be sorted ascending.
    """
    if not timestamps:
        return []
    tolerance = timedelta(seconds=tolerance_s)
    anchors: list[datetime] = []
    cluster_last = timestamps[0]
    for ts in timestamps[1:]:
        if ts - cluster_last <= tolerance:
            cluster_last = ts
        else:
            anchors.append(cluster_last)
            cluster_last = ts
    anchors.append(cluster_last)
    return anchors


def _state_at(snapshots: list[StateSnapshot], ts: datetime) -> str | None:
    """Return state value at-or-just-before `ts`; None if no prior snapshot."""
    val: str | None = None
    for snap in snapshots:
        if snap.timestamp <= ts:
            val = snap.value
        else:
            break
    return val


# --- synthesized current hour -------------------------------------------


def _current_row(
    now: datetime,
    tz: tzinfo,
    current_obs: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Synthesize a row for the current clock hour from live observations.

    The cache-sourced fields (visibility, convection, rainfall amount,
    duration) come from `current_obs` too — the wetteronline weather
    entity merges them in via `_synthesize_current_hour_forecast` and we
    consume the same dict shape.
    """
    if not current_obs:
        return None
    hour_dt = now.replace(minute=0, second=0, microsecond=0).astimezone(tz)
    row = _empty_row(hour_dt, SOURCE_CURRENT)
    row["condition_custom"] = current_obs.get("condition_custom")
    row["precipitation_probability"] = _to_number(
        current_obs.get("precipitation_probability")
    )
    row["precipitation_amount_mm_min"] = _to_number(
        current_obs.get("precipitation_amount_mm_min")
    )
    row["precipitation_amount_mm_max"] = _to_number(
        current_obs.get("precipitation_amount_mm_max")
    )
    row["precipitation_duration_min_min"] = _to_number(
        current_obs.get("precipitation_duration_min_min")
    )
    row["precipitation_duration_min_max"] = _to_number(
        current_obs.get("precipitation_duration_min_max")
    )
    row["convection_probability"] = _to_number(
        current_obs.get("convection_probability")
    )
    row["visibility_meter"] = _to_number(current_obs.get("visibility_meter"))
    _enrich_with_multiplier(row)
    return row


# --- nowcast (15-min sub-hour) ------------------------------------------


def _nowcast_rows(
    nowcast_items: list[dict[str, Any]], target_date: date
) -> list[dict[str, Any]]:
    """One row per 15-min nowcast slot on `target_date`.

    Nowcast items already pre-filtered upstream (wetteronline drops slots
    with `dt <= fetched_at`). Items carry only condition + precipitation
    prob/type — other fields stay None and dedupe will likely collapse
    same-condition runs.
    """
    rows: list[dict[str, Any]] = []
    for item in nowcast_items:
        try:
            ts = datetime.fromisoformat(item["date"])
        except (KeyError, ValueError):
            continue
        if ts.date() != target_date:
            continue
        row = _empty_row(ts, SOURCE_NOWCAST)
        row["condition_custom"] = item.get("condition_custom")
        row["precipitation_probability"] = _to_number(
            item.get("precipitation_probability")
        )
        _enrich_with_multiplier(row)
        rows.append(row)
    return rows


# --- hourly forecast (future hours beyond nowcast) ----------------------


def _forecast_rows(
    forecast_hours: list[dict[str, Any]],
    target_date: date,
    now: datetime,
) -> list[dict[str, Any]]:
    """Future hourly forecast rows (strictly after `now`'s clock hour).

    Skip hours covered by nowcast — those are emitted separately at
    finer granularity. With the current setup the only hour that
    overlaps both is the next round hour (e.g., 13:00 when fetch was at
    12:17 → nowcast covers 12:30..14:00, hours[0]=13:00). We accept the
    coarse hourly row plus the granular nowcast rows side-by-side; the
    dedupe pass keeps only rows where fields differ.
    """
    rows: list[dict[str, Any]] = []
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    for item in forecast_hours:
        raw_dt = item.get("datetime")
        if not raw_dt:
            continue
        try:
            ts = datetime.fromisoformat(raw_dt)
        except ValueError:
            continue
        if ts.date() != target_date:
            continue
        if ts <= current_hour:
            continue
        row = _empty_row(ts, SOURCE_FORECAST)
        row["condition_custom"] = item.get("condition_custom")
        row["precipitation_probability"] = _to_number(
            item.get("precipitation_probability")
        )
        row["precipitation_amount_mm_min"] = _to_number(
            item.get("precipitation_amount_mm_min")
        )
        row["precipitation_amount_mm_max"] = _to_number(
            item.get("precipitation_amount_mm_max")
        )
        row["precipitation_duration_min_min"] = _to_number(
            item.get("precipitation_duration_min_min")
        )
        row["precipitation_duration_min_max"] = _to_number(
            item.get("precipitation_duration_min_max")
        )
        row["convection_probability"] = _to_number(item.get("convection_probability"))
        row["visibility_meter"] = _to_number(item.get("visibility_meter"))
        _enrich_with_multiplier(row)
        rows.append(row)
    return rows


# --- helpers ------------------------------------------------------------


def _empty_row(ts: datetime, source: str) -> dict[str, Any]:
    return {
        "datetime": ts.isoformat(),
        "time": ts.strftime("%H:%M"),
        "source": source,
        "condition_custom": None,
        "precipitation_probability": None,
        "precipitation_amount_mm_min": None,
        "precipitation_amount_mm_max": None,
        "precipitation_duration_min_min": None,
        "precipitation_duration_min_max": None,
        "convection_probability": None,
        "visibility_meter": None,
    }


def _enrich_with_multiplier(row: dict[str, Any]) -> None:
    breakdown = compute_multiplier(
        row.get("precipitation_probability"),
        row.get("precipitation_amount_mm_max"),
        row.get("precipitation_duration_min_max"),
    )
    row["coverage"] = breakdown.coverage
    row["heaviness"] = breakdown.heaviness
    row["penalty"] = breakdown.penalty
    row["multiplier"] = breakdown.multiplier


def _to_number(value: Any) -> float | None:
    """Parse a raw HA state string (or already-numeric) into float|None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        if value in ("", "unknown", "unavailable", "None"):
            return None
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _dedupe_consecutive(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop consecutive rows where all `DEDUPE_FIELDS` are equal.

    `source`, `datetime`, `time` and computed columns are ignored — the
    8 input fields are the identity for dedupe purposes.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        if out and all(out[-1][f] == row[f] for f in DEDUPE_FIELDS):
            continue
        out.append(row)
    return out
