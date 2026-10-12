"""PV surplus — how much PV energy is left for the battery, hour by hour.

`surplus(h) = max(0, PV(h) - consumption(h))` — energy PV produces beyond what
the house uses in hour `h`, i.e. what can go into the battery (water heaters
only take what is left over, so consumption excludes them).

One hourly list, two readers summing different hours:
- the charge-start shift sums its price window [S, first hour pricier than S-1),
- the "room for sun" figure sums [actual charge start, start of T2).

Hourly, not half-hourly: prices, charge start and grid billing are hourly, and
hourly long-term statistics reach as far back as needed — the 5-minute ones
the Target SOC profiles use keep only 10 days.

Consumption comes from the same kind of day: workday from the last 10 workdays,
Saturday from the last 5 Saturdays, Sunday — and any other day off, which is
usually a church holiday that looks like a Sunday — from the last 5 Sundays.
The per-hour median of those days is the profile: one odd day (guests, oven)
does not move it, and spikes from different days do not stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from statistics import median
from typing import TYPE_CHECKING, Any, Final

from ..tariff.zones import evening_peak, is_holiday

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# Default consumption when no history is used — the same 0.9 kWh/h the
# Target SOC flat column assumes (0.45 kWh per 30 min).
FLAT_CONSUMPTION_KWH_PER_H: Final[float] = 0.9


class DayType(StrEnum):
    """Which earlier days a day's consumption is learned from."""

    WORKDAY = "workday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"

    @classmethod
    def of(cls, day: date) -> DayType:
        if day.weekday() == 6 or is_holiday(day):
            return cls.SUNDAY
        if day.weekday() == 5:
            return cls.SATURDAY
        return cls.WORKDAY

    @property
    def history_days(self) -> int:
        """Workdays come every day, weekends once a week — 5 keeps the season."""
        return 10 if self is DayType.WORKDAY else 5

    def previous_days(self, day: date) -> list[date]:
        """Return the `history_days` most recent days of this type before `day`."""
        found: list[date] = []
        candidate = day - timedelta(days=1)
        # Bounded walk: 5 Sundays fit in 5 weeks; holidays only add to the pool.
        while len(found) < self.history_days and (day - candidate).days <= 120:
            if DayType.of(candidate) is self:
                found.append(candidate)
            candidate -= timedelta(days=1)
        return found


def surplus_window_end(day: date) -> int:
    """First hour of the evening the battery must be full for (start of T2).

    Days off have no T2, but the evening still needs the battery (the evening
    plan sells in the dearest hours from 16:00), so the same seasonal hour holds.
    """
    return evening_peak(day).start


def median_by_hour(profiles: Iterable[Mapping[int, float]]) -> dict[int, float]:
    """Per-hour median over the given hourly consumption profiles."""
    by_hour: dict[int, list[float]] = {}
    for profile in profiles:
        for hour, kwh in profile.items():
            by_hour.setdefault(hour, []).append(kwh)
    return {hour: median(values) for hour, values in by_hour.items()}


@dataclass(frozen=True)
class HourSurplus:
    """One hour: PV, consumption and what is left for the battery (kWh)."""

    hour: int
    pv_kwh: float
    cons_kwh: float

    @property
    def surplus_kwh(self) -> float:
        return max(0.0, self.pv_kwh - self.cons_kwh)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hour": self.hour,
            "pv": round(self.pv_kwh, 3),
            "cons": round(self.cons_kwh, 3),
            "surplus": round(self.surplus_kwh, 3),
        }


@dataclass(frozen=True)
class SurplusProfile:
    """Hourly surplus of one day for one PV source and one consumption profile."""

    day: date
    hours: tuple[HourSurplus, ...]

    @classmethod
    def build(
        cls,
        day: date,
        pv_by_hour: Mapping[int, float],
        cons_by_hour: Mapping[int, float] | None,
    ) -> SurplusProfile:
        """Combine PV and consumption per hour; `None` consumption = flat 0.9."""
        hours = tuple(
            HourSurplus(
                hour=hour,
                pv_kwh=pv_by_hour.get(hour, 0.0),
                cons_kwh=(
                    FLAT_CONSUMPTION_KWH_PER_H
                    if cons_by_hour is None
                    else cons_by_hour.get(hour, FLAT_CONSUMPTION_KWH_PER_H)
                ),
            )
            for hour in range(24)
        )
        return cls(day=day, hours=hours)

    def total(self, start: int, end: int) -> float:
        """Surplus summed over hours `start` .. `end - 1`."""
        return sum(h.surplus_kwh for h in self.hours if start <= h.hour < end)

    def consumption(self, start: int, end: int) -> float:
        return sum(h.cons_kwh for h in self.hours if start <= h.hour < end)

    def pv(self, start: int, end: int) -> float:
        return sum(h.pv_kwh for h in self.hours if start <= h.hour < end)

    def window(self, start: int, end: int) -> list[dict[str, Any]]:
        return [h.to_dict() for h in self.hours if start <= h.hour < end]
