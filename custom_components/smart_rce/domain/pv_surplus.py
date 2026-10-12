"""PV surplus — how much PV energy is left for the battery, hour by hour.

`surplus(h) = max(0, PV(h) - consumption(h))` — energy PV produces beyond what
the house uses in hour `h`, i.e. what can go into the battery (water heaters
only take what is left over, so consumption excludes them).

One list of 30-min slots, two readers summing different windows:
- the charge-start shift sums its price window [S, first hour pricier than S-1),
- the "room for sun" figure sums [actual charge start, start of T2).

Half-hourly because the PV forecast is: an hour with a dull first half and a
sunny second one has a surplus the hourly sum would hide. Consumption history
stays hourly (hourly long-term statistics reach as far back as needed, the
5-minute ones keep only 10 days) and is spread evenly over the two slots.

Consumption comes from the same kind of day: workday from the last 10 workdays,
Saturday from the last 5 Saturdays, Sunday — and any other day off, which is
usually a church holiday that looks like a Sunday — from the last 5 Sundays.
The per-hour median of those days is the profile: one odd day (guests, oven)
does not move it, and spikes from different days do not stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from statistics import median
from typing import TYPE_CHECKING, Any, Final

from ..tariff.zones import evening_peak, is_holiday

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# Default consumption when no history is used — the same 0.9 kWh/h the
# Target SOC flat column assumes (0.45 kWh per 30 min).
FLAT_CONSUMPTION_KWH_PER_H: Final[float] = 0.9

SLOT_MINUTES: Final[int] = 30
SLOTS_PER_DAY: Final[int] = 24 * 60 // SLOT_MINUTES


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


def slots_from_hourly(by_hour: Mapping[int, float]) -> dict[int, float]:
    """Spread hourly energy evenly over the hour's two 30-min slots."""
    out: dict[int, float] = {}
    for hour, kwh in by_hour.items():
        out[2 * hour] = out[2 * hour + 1] = kwh / 2
    return out


def minute_of_day(moment: datetime) -> float:
    return moment.hour * 60 + moment.minute + moment.second / 60


@dataclass(frozen=True)
class SlotSurplus:
    """One 30-min slot: PV, consumption and what is left for the battery (kWh)."""

    slot: int
    pv_kwh: float
    cons_kwh: float

    @property
    def start_minute(self) -> int:
        return self.slot * SLOT_MINUTES

    @property
    def surplus_kwh(self) -> float:
        return max(0.0, self.pv_kwh - self.cons_kwh)

    def overlap(self, start: float, end: float) -> float:
        """Fraction of this slot inside `[start, end)` minutes of the day."""
        lo = max(start, self.start_minute)
        hi = min(end, self.start_minute + SLOT_MINUTES)
        return (hi - lo) / SLOT_MINUTES if hi > lo else 0.0


@dataclass(frozen=True)
class HourSurplus:
    """Hourly view of the slots: sums of PV, consumption and slot surpluses.

    The surplus is the sum of the two slots' surpluses, not `max(0, PV - cons)`
    of the hour — a sunny second half is not cancelled by a dull first one.
    """

    hour: int
    pv_kwh: float
    cons_kwh: float
    surplus_kwh: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "hour": self.hour,
            "pv": round(self.pv_kwh, 3),
            "cons": round(self.cons_kwh, 3),
            "surplus": round(self.surplus_kwh, 3),
        }


@dataclass(frozen=True)
class SurplusProfile:
    """Surplus of one day per 30-min slot, for one PV source and one consumption.

    Windows are given in minutes of the day: a charge start can be 9:30, or
    "now" at 8:41. A slot cut by a window edge counts in proportion.
    """

    day: date
    slots: tuple[SlotSurplus, ...]

    @classmethod
    def build(
        cls,
        day: date,
        pv_by_slot: Mapping[int, float],
        cons_by_slot: Mapping[int, float] | None,
    ) -> SurplusProfile:
        """Combine PV and consumption per slot; `None` consumption = flat 0.9 kWh/h."""
        flat = FLAT_CONSUMPTION_KWH_PER_H / 2
        slots = tuple(
            SlotSurplus(
                slot=slot,
                pv_kwh=pv_by_slot.get(slot, 0.0),
                cons_kwh=(
                    flat if cons_by_slot is None else cons_by_slot.get(slot, flat)
                ),
            )
            for slot in range(SLOTS_PER_DAY)
        )
        return cls(day=day, slots=slots)

    def total(self, start: float, end: float) -> float:
        """Surplus over `[start, end)` minutes of the day."""
        return sum(s.surplus_kwh * s.overlap(start, end) for s in self.slots)

    def remaining(self, start: float, end: float, now: datetime) -> float:
        """Surplus still to come in `[start, end)`, seen from `now`.

        Slots already gone count nothing, the current one only for the minutes
        left in it; before the day the whole window counts.
        """
        if now.date() < self.day:
            return self.total(start, end)
        if now.date() > self.day:
            return 0.0
        return self.total(max(start, minute_of_day(now)), end)

    def consumption(self, start: float, end: float) -> float:
        return sum(s.cons_kwh * s.overlap(start, end) for s in self.slots)

    def pv(self, start: float, end: float) -> float:
        return sum(s.pv_kwh * s.overlap(start, end) for s in self.slots)

    def hourly(self) -> tuple[HourSurplus, ...]:
        """Sum the slots per hour — for charts that stay hourly."""
        hours: list[HourSurplus] = []
        for hour in range(24):
            pair = self.slots[2 * hour : 2 * hour + 2]
            hours.append(
                HourSurplus(
                    hour=hour,
                    pv_kwh=sum(s.pv_kwh for s in pair),
                    cons_kwh=sum(s.cons_kwh for s in pair),
                    surplus_kwh=sum(s.surplus_kwh for s in pair),
                )
            )
        return tuple(hours)
