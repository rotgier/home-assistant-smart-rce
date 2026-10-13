#!/usr/bin/env python3
"""Rebuild `archive/forecast` day files for days before the archive existed.

Input: a raw recorder dump (home-assistant-ops `fetch_recorder_dump.py`):
`history_*`, `history_extra_*`, `history_extra2_*` (state history with
attributes) and `stats5_*` (5-minute statistics) per day.

For each moment the production `SnapshotSchedule` would have picked (20 s after
a Solcast fetch, grid fallback, 04:00:30), it rebuilds:
- `context` — every `ForecastContext` input as the recorder saw it at that
  moment. Approximate: production assembles it in memory from the same
  sources, but timing within the minute and a few in-memory details differ;
- `variants` — what production actually computed, read from the forecast
  sensors' `forecast` attribute (exact, as recorded).
Records are marked `source: "recorder_backfill"`; replay parity checks skip them.

It also replays the current strategies on each rebuilt context and prints how
close they come to the recorded outputs — the measure of how usable the
backfill is (current code ≠ code of that day for variants changed since).

Usage:
    .venv/bin/python scripts/forecast_archive_from_dump.py DUMP_DIR OUT_DIR DAY [DAY ...]
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import UTC, date, datetime, time, timedelta
import gzip
import json
from pathlib import Path
import statistics
import sys
from typing import Any
from zoneinfo import ZoneInfo

# Run as a plain script: make the repo root (custom_components/) importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.smart_rce.domain.pv_forecast import (  # noqa: E402
    AdjustedPeriod,
    ForecastContext,
    LivePvSignals,
    PvForecast,
    PvForecastResult,
    SolcastPeriod,
    WeatherConditionAtHour,
    WeatherConditions,
)
from custom_components.smart_rce.domain.pv_forecast.forecast_snapshot import (  # noqa: E402
    ForecastSnapshot,
    SnapshotTrigger,
    VariantOutcome,
)
from custom_components.smart_rce.domain.pv_forecast.snapshot_schedule import (  # noqa: E402
    SnapshotSchedule,
)
from custom_components.smart_rce.infrastructure.daily_jsonl_archive import (  # noqa: E402
    DailyJsonlArchive,
)

_TZ = ZoneInfo("Europe/Warsaw")
_SETTLE = timedelta(seconds=20)
_API = "sensor.solcast_pv_forecast_ostatnia_aktualizacja_api"
# Forecast sensor suffix → PvForecast key (sensor names come from pretty labels).
_SENSOR_TO_KEY = {
    "at_6": "at_6",
    "live": "live",
    "tomorrow_at_6": "tomorrow_at_6",
    "tomorrow_live": "tomorrow_live",
    "extrap_pattern": "extrapolated_live_pattern",
    "extrap_proportional": "extrapolated_live_proportional",
    "extrap_band": "extrapolated_live_band",
    "extrap_band_recent": "extrapolated_live_band_recent",
    "extrap_proportional_capped": "extrapolated_live_proportional_capped",
    "extrap_blend": "extrapolated_live_blend",
}
_EXTRAP_KEYS = {v.key for v in PvForecast if v.is_extrap}


class _States:
    """State history of one day: value and attributes in force at any moment."""

    def __init__(self, histories: dict[str, list[dict[str, Any]]]) -> None:
        self._rows: dict[str, tuple[list[float], list[tuple[str, dict]]]] = {}
        for entity_id, rows in histories.items():
            times: list[float] = []
            values: list[tuple[str, dict]] = []
            attrs: dict = {}
            for row in sorted(rows, key=lambda r: r.get("lu", r.get("lc", 0))):
                attrs = row.get("a", attrs)
                times.append(float(row.get("lu", row.get("lc", 0))))
                values.append((row.get("s"), attrs))
            self._rows[entity_id] = (times, values)

    def at(self, entity_id: str, moment: datetime) -> tuple[str | None, dict]:
        times, values = self._rows.get(entity_id, ([], []))
        index = bisect_right(times, moment.timestamp()) - 1
        return values[index] if index >= 0 else (None, {})

    def number(self, entity_id: str, moment: datetime) -> float | None:
        state, _ = self.at(entity_id, moment)
        try:
            return (
                float(state) if state not in (None, "unknown", "unavailable") else None
            )
        except ValueError:
            return None

    def changes(self, entity_id: str) -> list[tuple[datetime, str]]:
        times, values = self._rows.get(entity_id, ([], []))
        return [
            (datetime.fromtimestamp(t, tz=UTC).astimezone(_TZ), v[0])
            for t, v in zip(times, values, strict=True)
        ]


def main(dump_dir: Path, out_dir: Path, days: list[date]) -> None:
    archive = DailyJsonlArchive(out_dir)
    for day in days:
        records, quality = _rebuild_day(dump_dir, day)
        if not records:
            print(f"{day}: nothing to rebuild")
            continue
        archive.write_day(day, records)
        print(f"{day}: {len(records)} snapshots; {quality}")


def _rebuild_day(dump_dir: Path, day: date) -> tuple[list[dict[str, Any]], str]:
    histories: dict[str, list[dict[str, Any]]] = {}
    for prefix in ("history", "history_extra", "history_extra2"):
        path = dump_dir / f"{prefix}_{day}.json.gz"
        if path.exists():
            histories.update(json.loads(gzip.decompress(path.read_bytes())))
    stats_path = dump_dir / f"stats5_{day}.json.gz"
    if not histories or not stats_path.exists():
        return [], ""
    states = _States(histories)
    stats = json.loads(gzip.decompress(stats_path.read_bytes()))
    # Strategies keep their last result when an input is missing; without a
    # reset a day would inherit whatever day was replayed before it.
    for variant in PvForecast:
        variant.strategy.result = None
        variant.strategy.remaining_kwh = None
    errors: dict[str, list[float]] = {}
    records: list[dict[str, Any]] = []
    fetches = [m for m, _ in states.changes(_API)]
    for moment, trigger in _moments(states, day):
        dispatched = _last_dispatch(moment, fetches)
        context = _context(states, stats, day, dispatched)
        variants = _recorded_variants(states, moment)
        if not variants:
            continue
        snapshot = ForecastSnapshot(
            taken_at=moment,
            trigger=trigger,
            solcast_fetched_at=states.at(_API, moment)[0],
            code_version="unknown",
            context=context,
            variants=variants,
            source="recorder_backfill",
        )
        records.append(snapshot.to_dict())
        _replay_errors(context, variants, errors)
    quality = "; ".join(
        f"{key.removeprefix('extrapolated_live_')}: exact {_exact_share(values):.0%}"
        f" median {statistics.median(values):.3f} max {max(values):.3f}"
        for key, values in sorted(errors.items())
    )
    return records, f"replay vs recorded |Δ total|/total — {quality}"


def _exact_share(values: list[float]) -> float:
    return sum(value < 1e-9 for value in values) / len(values)


def _last_dispatch(moment: datetime, fetches: list[datetime]) -> datetime:
    """When production last built the context a snapshot at `moment` captures.

    Every minute tick (:00) dispatches; so does a Solcast update, which is what
    a fetch-driven snapshot 20 s later sees.
    """
    tick = moment.replace(second=0, microsecond=0)
    recent = [f for f in fetches if tick < f <= moment]
    return max(recent) if recent else tick


def _moments(states: _States, day: date) -> list[tuple[datetime, SnapshotTrigger]]:
    """Replay the production schedule over the day's events."""
    schedule = SnapshotSchedule()
    events: list[tuple[datetime, str, str | None]] = []
    for index, (moment, value) in enumerate(states.changes(_API)):
        if moment.date() == day:
            events.append((moment, "first" if index == 0 else "fetch", value))
    events.extend(
        (datetime.combine(day, time(hour, minute, 30), _TZ), "grid", None)
        for hour in range(24)
        for minute in (11, 41)
    )
    events.append((datetime.combine(day, time(4, 0, 30), _TZ), "morning", None))
    pending: list[datetime] = []
    moments: list[tuple[datetime, SnapshotTrigger]] = []
    for moment, kind, value in sorted(events, key=lambda e: e[0]):
        while pending and pending[0] <= moment:
            due = pending.pop(0)
            moments.append((due, SnapshotTrigger.SOLCAST))
            schedule.taken(due)
            schedule.fetch_handled()
        if kind == "first":
            schedule.remember_fetch(value)
        elif kind == "fetch" and schedule.accept_fetch(value, moment):
            pending.append(moment + _SETTLE)
        elif kind == "grid" and schedule.grid_due(moment):
            moments.append((moment, SnapshotTrigger.GRID))
            schedule.taken(moment)
        elif kind == "morning":
            moments.append((moment, SnapshotTrigger.MORNING_CHARGE))
            schedule.taken(moment)
    moments.extend((due, SnapshotTrigger.SOLCAST) for due in pending)
    return sorted(moments, key=lambda m: m[0])


def _context(
    states: _States, stats: dict[str, list[dict]], day: date, moment: datetime
) -> ForecastContext:
    stable, _ = states.at("binary_sensor.pv_derivative_is_stable", moment)
    override, _ = states.at("time.ems_battery_charge_start_hour_override", moment)
    at_6_entity, at_6_attr = (
        ("sensor.solcast_pv_forecast_prognoza_na_dzisiaj", "detailedForecast")
        if moment.time() < time(6, 2)
        else ("sensor.solcast_forecast_at_6", "forecast")
    )
    return ForecastContext(
        now=moment,
        signals=LivePvSignals(
            pv_power_w=states.number("sensor.pv_power_avg_5_minutes", moment),
            bucket_so_far_kwh=states.number(
                "sensor.total_pv_generation_bi_hourly", moment
            ),
            derivative_w_per_min=states.number(
                "sensor.pv_power_derivative_avg_2min", moment
            ),
            stability_stable=(
                None if stable in (None, "unknown", "unavailable") else stable == "on"
            ),
        ),
        weather=_weather(states, day, moment),
        solcast_at_6=_solcast(states, at_6_entity, at_6_attr, moment),
        solcast_today=_solcast(
            states,
            "sensor.solcast_pv_forecast_prognoza_na_dzisiaj",
            "detailedForecast",
            moment,
        ),
        solcast_tomorrow=_solcast(
            states,
            "sensor.solcast_pv_forecast_prognoza_na_jutro",
            "detailedForecast",
            moment,
        ),
        realized_pv_today=_realized(stats, moment),
        consumption_w=states.number(
            "sensor.house_consumption_minus_water_avg_5_minutes", moment
        ),
        start_charge_hour=(
            int(override.split(":")[0])
            if override not in (None, "unknown", "unavailable")
            else None
        ),
    )


def _weather(states: _States, day: date, moment: datetime) -> WeatherConditions:
    _, history_attrs = states.at("sensor.rce_weather_forecast_history", moment)
    history = [
        WeatherConditionAtHour(
            hour=int(hour), condition_custom=condition, forecast_date=day
        )
        for hour, condition in history_attrs.get("hours", {}).items()
    ]
    forecast: list[WeatherConditionAtHour] = []
    for entity_id in (
        "sensor.wetteronline_forecast_for_today",
        "sensor.wetteronline_forecast_for_tomorrow",
    ):
        _, attrs = states.at(entity_id, moment)
        for item in attrs.get("forecast", []):
            when = datetime.fromisoformat(item["datetime"])
            forecast.append(
                WeatherConditionAtHour(
                    hour=when.hour,
                    condition_custom=item.get("condition_custom", "cloudy"),
                    forecast_date=when.date(),
                )
            )
    return WeatherConditions.from_history_and_forecast(history, forecast)


def _solcast(
    states: _States, entity_id: str, attr: str, moment: datetime
) -> list[SolcastPeriod]:
    _, attrs = states.at(entity_id, moment)
    return [
        SolcastPeriod(
            # In HA the attribute holds datetimes; the reader stores str(datetime).
            period_start=str(datetime.fromisoformat(str(item["period_start"]))),
            pv_estimate=item["pv_estimate"],
            pv_estimate10=item["pv_estimate10"],
            pv_estimate90=item["pv_estimate90"],
        )
        for item in attrs.get(attr, [])
    ]


def _realized(
    stats: dict[str, list[dict]], moment: datetime
) -> dict[tuple[int, int], float]:
    """Return closed 30-min buckets as `RealizedPvLoader` saw them (:00:30/:30:30)."""
    realized: dict[tuple[int, int], float] = {}
    for row in stats.get("sensor.total_pv_generation_bi_hourly", []):
        start = datetime.fromtimestamp(row["start"] / 1000, tz=UTC).astimezone(_TZ)
        if start.minute not in (25, 55) or row.get("state") is None:
            continue
        bucket_end = start + timedelta(minutes=5)
        if bucket_end + timedelta(seconds=30) <= moment:
            realized[(start.hour, 0 if start.minute == 25 else 30)] = float(
                row["state"]
            )
    return realized


def _recorded_variants(states: _States, moment: datetime) -> dict[str, VariantOutcome]:
    variants: dict[str, VariantOutcome] = {}
    for suffix, key in _SENSOR_TO_KEY.items():
        state, attrs = states.at(f"sensor.rce_pv_forecast_{suffix}", moment)
        periods = attrs.get("forecast")
        if not periods or state in (None, "unknown", "unavailable"):
            continue
        forecast = [
            AdjustedPeriod(
                period_start=p["period_start"],
                pv_estimate_adjusted=p["pv_estimate_adjusted"],
            )
            for p in periods
        ]
        total = round(sum(p.pv_estimate_adjusted for p in forecast) / 2, 4)
        result = PvForecastResult(
            forecast=forecast, total_kwh=total if key in _EXTRAP_KEYS else float(state)
        )
        variants[key] = VariantOutcome(
            result=result,
            remaining_kwh=(
                float(state)
                if key in _EXTRAP_KEYS
                else result.remaining_kwh_from(moment)
            ),
        )
    return variants


def _replay_errors(
    context: ForecastContext,
    recorded: dict[str, VariantOutcome],
    errors: dict[str, list[float]],
) -> None:
    for variant in PvForecast:
        variant.strategy.update(context)
    for variant in PvForecast:
        stored = recorded.get(variant.key)
        if stored is None or stored.result is None or variant.result is None:
            continue
        expected = stored.result.total_kwh
        if expected > 0.5:
            errors.setdefault(variant.key, []).append(
                abs(variant.result.total_kwh - expected) / expected
            )


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    main(
        Path(sys.argv[1]),
        Path(sys.argv[2]),
        [date.fromisoformat(arg) for arg in sys.argv[3:]],
    )
