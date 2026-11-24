"""Observed weather of a day — raw state changes and hourly rows (from 24.11.2026).

For the heating analyses: wind and the outside temperature kept beyond the
recorder's 10 days (user's request 24.11.2026).

- Raw: each entity's state changes as the recorder has them — time and value
  (a number, or text for a text sensor such as the precipitation type), None
  for unavailable/unknown (an outage stays visible). The recorder writes
  no row while a value repeats, so a series is change points, not samples.
  `in_force_at_start` is the state carried in from before midnight: no
  reading at 00:00, only what held then.
- Hourly rows are time-weighted on the value in force (a sample mean would
  leave most hours empty — the wind changes a few times a day). Speed:
  mean and max; direction: the mean of unit vectors weighted by speed × time,
  normalized to 0-360 only here (the raw bearing is kept as recorded);
  steadiness = length of the mean vector / mean speed (1 = one direction the
  whole hour). Calm leaves direction and steadiness None; winds cancelling
  out give steadiness 0 and no direction. The bearing is the direction the wind comes FROM (270 = from the west).
  The other measures (`MEASURES`: the house's outside temperature — the main
  one, measured here — and WetterOnline's model values): time-weighted mean,
  min and max of the values in force. A value never holds across an outage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
import math
from typing import TYPE_CHECKING, Any, ClassVar, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

WIND_SPEED: Final = "wind_speed"
WIND_BEARING: Final = "wind_bearing"
WIND_GUST: Final = "wind_gust"  # WetterOnline's strongest gust (`max_gust`)
OUTSIDE_TEMP: Final = "outside_temp"
WO_TEMPERATURE: Final = "wo_temperature"
WO_APPARENT: Final = "wo_apparent_temperature"
WO_HUMIDITY: Final = "wo_humidity"
WO_DEW_POINT: Final = "wo_dew_point"
WO_PRESSURE: Final = "wo_pressure"
WO_VISIBILITY: Final = "wo_visibility"
WO_PRECIPITATION_TYPE: Final = "wo_precipitation_type"  # text, raw only
# Role -> the hourly row's key prefix (`<prefix>_mean`, `_min`, `_max`, `_cover_min`).
MEASURES: Final[dict[str, str]] = {
    WIND_GUST: "wind_gust_kmh",
    OUTSIDE_TEMP: "temp_c",
    WO_TEMPERATURE: "wo_temp_c",
    WO_APPARENT: "wo_apparent_c",
    WO_HUMIDITY: "wo_humidity_pct",
    WO_DEW_POINT: "wo_dew_point_c",
    WO_PRESSURE: "wo_pressure_hpa",
    WO_VISIBILITY: "wo_visibility_m",
}


@dataclass(frozen=True)
class ObservedWeatherDay:
    """One local day of observed weather: raw series by role, hourly rows on demand."""

    # Bump when the hourly rows are computed differently: the raw series stay
    # the source of truth, rows of an older version can be recomputed.
    VERSION: ClassVar[int] = 1
    _SECTORS: ClassVar[tuple[str, ...]] = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    # Below this share of the mean speed the mean vector has no direction.
    _CALM_STEADINESS: ClassVar[float] = 1e-6

    day: date
    tz: tzinfo
    series: Mapping[str, ObservedSeries]  # role -> series

    def hours(self) -> tuple[HourlyWeather, ...]:
        """Return the day's hourly rows — 23 or 25 on a DST change day."""
        return tuple(
            self._hour(start, start + timedelta(hours=1))
            for start in self._hour_starts()
        )

    def _hour_starts(self) -> list[datetime]:
        """Return the starts of the day's real hours (UTC steps, local labels)."""
        start = datetime.combine(self.day, time(0), self.tz).astimezone(UTC)
        end = datetime.combine(self.day + timedelta(days=1), time(0), self.tz)
        starts = []
        while start < end.astimezone(UTC):
            starts.append(start.astimezone(self.tz))
            start += timedelta(hours=1)
        return starts

    def _hour(self, start: datetime, end: datetime) -> HourlyWeather:
        return HourlyWeather(
            start=start,
            wind=self._wind(start, end),
            measures={
                role: self._measure(role, start, end)
                for role in MEASURES
                if role in self.series
            },
        )

    def _wind(self, start: datetime, end: datetime) -> HourlyWind:
        """Pair speed with the bearing in force, then weigh by speed × time."""
        speed = self.series.get(WIND_SPEED)
        bearing = self.series.get(WIND_BEARING)
        cuts = sorted(
            {start, end}
            | {
                p.at
                for s in (speed, bearing)
                if s is not None
                for p in s.points
                if start < p.at < end
            }
        )
        seconds = weighted = east = north = 0.0
        top: float | None = None
        for a, b in zip(cuts, cuts[1:], strict=False):
            v = speed.number_at(a) if speed is not None else None
            d = bearing.number_at(a) if bearing is not None else None
            if v is None or d is None:
                continue
            span = (b - a).total_seconds()
            seconds += span
            weighted += v * span
            east += v * span * math.sin(math.radians(d))
            north += v * span * math.cos(math.radians(d))
            top = v if top is None else max(top, v)
        if not seconds:
            return HourlyWind(None, None, None, None, None, 0)
        mean = weighted / seconds
        direction: int | None = None
        steadiness: float | None = None
        if weighted:  # calm (0 km/h all hour): no direction, no steadiness
            share = math.hypot(east, north) / weighted
            steadiness = round(share, 2)
            if share >= self._CALM_STEADINESS:  # else the winds cancel out
                direction = round(math.degrees(math.atan2(east, north)) % 360) % 360
        return HourlyWind(
            mean_kmh=round(mean, 1),
            max_kmh=top,
            direction_deg=direction,
            direction=self._sector(direction),
            steadiness=steadiness,
            cover_min=round(seconds / 60),
        )

    @classmethod
    def _sector(cls, direction: int | None) -> str | None:
        """Return the 8-point compass word of `direction` (sectors centred on N, NE…)."""
        if direction is None:
            return None
        return cls._SECTORS[round(direction / 45) % 8]

    def _measure(self, role: str, start: datetime, end: datetime) -> HourlyMeasure:
        """Return the time-weighted mean, min and max of `role` over [start, end)."""
        series = self.series[role]
        cuts = sorted(
            {start, end} | {p.at for p in series.points if start < p.at < end}
        )
        held: list[tuple[float, float]] = []  # (seconds, value)
        for a, b in zip(cuts, cuts[1:], strict=False):
            value = series.number_at(a)
            if value is not None:
                held.append(((b - a).total_seconds(), value))
        seconds = sum(span for span, _ in held)
        return HourlyMeasure(
            mean=round(sum(span * v for span, v in held) / seconds, 2)
            if seconds
            else None,
            min=min(v for _, v in held) if held else None,
            max=max(v for _, v in held) if held else None,
            cover_min=round(seconds / 60),
        )

    # --- persistence --- #

    def to_records(self) -> list[dict[str, Any]]:
        """Return the day's archive lines: header, raw series, hourly rows."""
        return [
            {
                "kind": "meta",
                "day": self.day.isoformat(),
                "version": self.VERSION,
                "bearing": "direction the wind comes from, degrees 0-360",
                "hourly": "time-weighted on the value in force; direction = mean"
                " of unit vectors weighted by speed x time",
            },
            *(s.to_record(role) for role, s in self.series.items()),
            *(h.to_record() for h in self.hours()),
        ]

    @classmethod
    def from_records(
        cls, day: date, tz: tzinfo, records: Iterable[Mapping[str, Any]]
    ) -> ObservedWeatherDay:
        """Rebuild the day from its archive lines (the raw series; rows recomputed)."""
        return cls(
            day,
            tz,
            {
                str(r["role"]): ObservedSeries.from_record(r)
                for r in records
                if r.get("kind") == "raw"
            },
        )


@dataclass(frozen=True)
class HourlyWeather:
    """One hour: wind and the measures, time-weighted (None = no data)."""

    start: datetime
    wind: HourlyWind
    measures: Mapping[str, HourlyMeasure]  # role -> the hour's figures

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "kind": "hour",
            "hour": self.start.isoformat(),
            "wind_kmh_mean": self.wind.mean_kmh,
            "wind_kmh_max": self.wind.max_kmh,
            "wind_dir_deg": self.wind.direction_deg,
            "wind_dir": self.wind.direction,
            "wind_steadiness": self.wind.steadiness,
            "wind_cover_min": self.wind.cover_min,
        }
        for role, measure in self.measures.items():
            prefix = MEASURES[role]
            record |= {
                f"{prefix}_mean": measure.mean,
                f"{prefix}_min": measure.min,
                f"{prefix}_max": measure.max,
                f"{prefix}_cover_min": measure.cover_min,
            }
        return record


@dataclass(frozen=True)
class HourlyWind:
    """The hour's wind; direction and steadiness None in calm or cancelling winds."""

    mean_kmh: float | None
    max_kmh: float | None
    direction_deg: int | None  # where the wind comes from, 0-359
    direction: str | None  # N, NE, E, SE, S, SW, W, NW
    steadiness: float | None  # |mean vector| / mean speed, 0-1
    cover_min: int  # minutes with both speed and bearing in force


@dataclass(frozen=True)
class HourlyMeasure:
    """One measure over the hour (None = nothing in force)."""

    mean: float | None
    min: float | None
    max: float | None
    cover_min: int  # minutes with a value in force


@dataclass(frozen=True)
class ObservedSeries:
    """One entity's state changes over the day, as recorded."""

    entity_id: str
    unit: str | None
    in_force_at_start: float | str | None  # carried in from before midnight
    points: tuple[SeriesPoint, ...]  # changes after midnight, in time order

    def number_at(self, moment: datetime) -> float | None:
        """Return the number in force at `moment` (None: unknown, an outage, text)."""
        found = self.in_force_at_start
        for point in self.points:
            if point.at > moment:
                break
            found = point.value
        return float(found) if isinstance(found, int | float) else None

    def to_record(self, role: str) -> dict[str, Any]:
        return {
            "kind": "raw",
            "role": role,
            "entity_id": self.entity_id,
            "unit": self.unit,
            "in_force_at_start": self.in_force_at_start,
            "points": [[p.at.isoformat(), p.value] for p in self.points],
        }

    @classmethod
    def from_record(cls, data: Mapping[str, Any]) -> ObservedSeries:
        return cls(
            entity_id=str(data["entity_id"]),
            unit=data.get("unit"),
            in_force_at_start=data.get("in_force_at_start"),
            points=tuple(
                SeriesPoint(datetime.fromisoformat(at), value)
                for at, value in data.get("points", [])
            ),
        )

    @classmethod
    def of(
        cls,
        entity_id: str,
        unit: str | None,
        rows: Sequence[tuple[datetime, float | str | None]],
        day_start: datetime,
    ) -> ObservedSeries:
        """Split recorded rows into the state carried in and the day's changes."""
        before = [v for at, v in rows if at <= day_start]
        return cls(
            entity_id,
            unit,
            before[-1] if before else None,
            tuple(SeriesPoint(at, v) for at, v in rows if at > day_start),
        )


@dataclass(frozen=True)
class SeriesPoint:
    """One recorded state: when it was set and its value (None = unavailable)."""

    at: datetime
    value: float | str | None
