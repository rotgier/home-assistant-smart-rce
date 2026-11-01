"""Weather at a moment — observed until it, the forecast as it stood at it after.

The chart under the accuracy table shows, for a moment (09:41), what the
weather really did until then and what WetterOnline forecast from then on —
the same view as Target SOC Historical's snapshot chart
(`dashboards/lib/weather.py`, `weather_at_moment_js`):

- observed: the changes of `sensor.wetteronline_condition_custom` up to the
  moment, at their own minutes (the steps of a showery morning);
- forecast: `sensor.wetteronline_forecast_for_today` as it stood at the moment
  (its last state up to it) — the 15-minute nowcast where it has one, the
  hours after that.

The recorder keeps both for 10 days, so a day's inputs are archived
(`WeatherInputs`, kept as read) and the points derived from them here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

KIND = "moment_weather"


@dataclass(frozen=True)
class WeatherInputs:
    """One day's weather as the recorder had it (value object, archived).

    `observed`: (when, condition) changes over the day, the state in force at
    midnight first. `forecasts`: per moment label, the forecast in force at it
    — hourly (start, condition) and nowcast (start, condition) of the day.
    """

    day: date
    observed: tuple[tuple[datetime, str], ...]
    forecasts: Mapping[str, MomentForecast]

    def points(self, label: str, at: datetime) -> list[tuple[datetime, str]]:
        """Return the weather as known at `at`: observed until it, forecast after.

        Nowcast quarters after the moment first; an hour they cover is not
        taken again from the hourly forecast, nor is an hour that started
        before the moment (the observed state holds until its next quarter).
        """
        out = [(when, cond) for when, cond in self.observed if when <= at]
        forecast = self.forecasts.get(label)
        if forecast is None:
            return out
        quarters = [(when, cond) for when, cond in forecast.nowcast if when > at]
        covered = {when.hour for when, _cond in quarters}
        hours = [
            (when, cond)
            for when, cond in forecast.hourly
            if when > at and when.hour not in covered
        ]
        return sorted(out + quarters + hours, key=lambda point: point[0])

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": KIND,
            "day": self.day.isoformat(),
            "observed": [[when.isoformat(), cond] for when, cond in self.observed],
            "forecasts": {
                label: forecast.to_dict() for label, forecast in self.forecasts.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WeatherInputs:
        return cls(
            day=date.fromisoformat(data["day"]),
            observed=_pairs(data.get("observed", [])),
            forecasts={
                label: MomentForecast.from_dict(raw)
                for label, raw in (data.get("forecasts") or {}).items()
            },
        )


@dataclass(frozen=True)
class MomentForecast:
    """The forecast in force at one moment, the day's entries only."""

    hourly: tuple[tuple[datetime, str], ...]
    nowcast: tuple[tuple[datetime, str], ...]

    @classmethod
    def from_forecast(
        cls, items: Sequence[Mapping[str, Any]], day: date
    ) -> MomentForecast:
        """Keep the day's hours and nowcast quarters of the sensor's `forecast`."""
        hourly: list[tuple[datetime, str]] = []
        nowcast: list[tuple[datetime, str]] = []
        for item in items:
            start = datetime.fromisoformat(str(item["datetime"]))
            if start.date() == day:
                cond = item.get("condition_custom") or item.get("condition")
                hourly.append((start, str(cond)))
            for quarter in item.get("nowcast_15min") or ():
                when = datetime.fromisoformat(str(quarter["date"]))
                if when.date() == day:
                    nowcast.append((when, str(quarter.get("condition_custom"))))
        return cls(tuple(hourly), tuple(nowcast))

    def to_dict(self) -> dict[str, Any]:
        return {
            "hourly": [[when.isoformat(), cond] for when, cond in self.hourly],
            "nowcast": [[when.isoformat(), cond] for when, cond in self.nowcast],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MomentForecast:
        return cls(_pairs(data.get("hourly", [])), _pairs(data.get("nowcast", [])))


def _pairs(raw: Sequence[Sequence[str]]) -> tuple[tuple[datetime, str], ...]:
    return tuple((datetime.fromisoformat(when), str(cond)) for when, cond in raw)
