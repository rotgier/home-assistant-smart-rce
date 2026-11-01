"""Moment charts — the state at each moment of the accuracy table, for a chart.

At a moment (04:41 … 09:41) production read one snapshot (`consumed`, the
same the table scores). The chart shows what that snapshot said — every PV
variant, Solcast's p10/p90 — against what came over the whole day: PV and
consumption, and the consumption model the system counted with. The weather:
observed until the moment, the forecast as it stood at it after
(`moment_weather`); without those inputs the snapshot's hourly conditions.

Built when a day is read (from the forecast and energy archives), not kept in
the score record. The card only draws: timestamps, units and the weather's
scale come ready from here (`MOMENT_CHARTS_SCHEMA` versions the contract).
"""

from __future__ import annotations

from datetime import date, datetime, time, tzinfo
from typing import TYPE_CHECKING, Any, Final

from .day_actuals import CONSUMPTION, PV
from .reserve_score import RESERVE_MOMENTS
from .start_score import forecast_half_hours
from .variant_results import SCORED_VARIANTS, consumed

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .day_actuals import DayActuals
    from .moment_weather import WeatherInputs
    from .variant_results import ScoredSnapshot

MOMENT_CHARTS_SCHEMA: Final = 1
# Half hours drawn: 04:00 to 21:00 (the sun's longest day).
FIRST_SLOT: Final = 8
END_SLOT: Final = 42
# The weather on the chart's reversed axis — the scale of the RCE Forecast
# chart (`dashboards/views/rce_forecast.py`, `condMap`): sunny on top.
WEATHER_LEVEL: Final[Mapping[str, int]] = {
    "sunny": 100,
    "clear-night": 100,
    "partlycloudy-variable": 80,
    "exceptional": 80,
    "partlycloudy": 60,
    "cloudy": 40,
    "rainy-light": 20,
    "pouring-light": 20,
    "pouring-light-partlycloudy": 20,
    "rainy": 10,
    "pouring": 10,
}
_UNKNOWN_WEATHER: Final = 40


def moment_charts(
    actuals: DayActuals,
    snapshots: Sequence[ScoredSnapshot],
    consumption_model: Mapping[int, float] | None,
    weather: WeatherInputs | None = None,
) -> dict[str, Any]:
    """Build the chart of every moment of the day (power in kW, epoch ms).

    `consumption_model`: the system's model per half hour (kWh, key 0..47),
    None when it had no earlier days (flat). `weather`: the day's observed
    weather and forecasts at the moments (None: the snapshots' hourly ones).
    """
    day, tz = actuals.day, actuals.tz
    slots = range(FIRST_SLOT, END_SLOT)
    moments: dict[str, Any] = {}
    for moment in RESERVE_MOMENTS:
        at = datetime.combine(day, moment.at, tz)
        snapshot = consumed(snapshots, at)
        if snapshot is not None:
            moments[moment.label] = _moment(snapshot, at, day, tz, slots)
            if weather is not None and (points := weather.points(moment.label, at)):
                moments[moment.label]["weather"] = [
                    [
                        int(when.timestamp() * 1000),
                        WEATHER_LEVEL.get(cond, _UNKNOWN_WEATHER),
                    ]
                    for when, cond in points
                ]
    return {
        "schema": MOMENT_CHARTS_SCHEMA,
        "unit": "kW",
        "times": [_epoch_ms(day, slot, tz) for slot in slots],
        "pv": _series(actuals.half_hours(PV), slots),
        "consumption": _series(actuals.half_hours(CONSUMPTION), slots),
        "consumption_model": (
            _series(consumption_model, slots) if consumption_model else None
        ),
        "moments": moments,
    }


def _moment(
    snapshot: ScoredSnapshot,
    at: datetime,
    day: date,
    tz: tzinfo,
    slots: range,
) -> dict[str, Any]:
    """Return what one snapshot said: every variant, Solcast's spread, the weather."""
    variants = {
        key: _series(forecast_half_hours(result, day), slots)
        for key in SCORED_VARIANTS
        if (result := snapshot.result(key)) is not None
    }
    p10: dict[int, float] = {}
    p90: dict[int, float] = {}
    for period in snapshot.context.solcast_today:
        start = datetime.fromisoformat(period.period_start)
        if start.date() == day:
            slot = start.hour * 2 + start.minute // 30
            # Solcast gives rates (kWh/h): half an hour's energy is half of it.
            p10[slot] = period.pv_estimate10 / 2
            p90[slot] = period.pv_estimate90 / 2
    return {
        "at": int(at.timestamp() * 1000),
        "snapshot_at": snapshot.taken_at.isoformat(),
        "variants": variants,
        "p10": _series(p10, slots),
        "p90": _series(p90, slots),
        "weather": _weather(snapshot, day, tz),
    }


def _weather(snapshot: ScoredSnapshot, day: date, tz: tzinfo) -> list[list[int]]:
    """Return the day's hourly conditions as [epoch ms, level]; a dated one wins."""
    by_hour: dict[int, str] = {}
    for c in snapshot.context.weather.conditions:
        if c.forecast_date == day or (
            c.forecast_date is None and c.hour not in by_hour
        ):
            by_hour[c.hour] = c.condition_custom
    return [
        [
            int(datetime.combine(day, time(hour), tz).timestamp() * 1000),
            WEATHER_LEVEL.get(by_hour[hour], _UNKNOWN_WEATHER),
        ]
        for hour in sorted(by_hour)
    ]


def _series(by_slot: Mapping[int, float], slots: range) -> list[float | None]:
    """Return the half hours' energy as average power (kW); None = no value."""
    return [round(by_slot[slot] * 2, 3) if slot in by_slot else None for slot in slots]


def _epoch_ms(day: date, slot: int, tz: tzinfo) -> int:
    """Return the wall-clock half hour's start (a DST day keeps its labels)."""
    start = datetime.combine(day, time(slot // 2, 30 * (slot % 2)), tz)
    return int(start.timestamp() * 1000)
