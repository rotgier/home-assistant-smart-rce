"""DayActuals — what really happened on a day, in the shapes the decisions read.

Wraps one `EnergyDay` (5-minute slots) with the zone it is read in. Half hours
come from `EnergyDay.energy_by_half_hour`, the one place 5-minute slots are
folded; hours and points in time are wall clock in that zone, so a DST day
lines up with the clock the decisions ran on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Final

from ..bucket import PV_WINDOW_HOURS, Bucket, Buckets

if TYPE_CHECKING:
    from ..energy_day import EnergyDay

PV: Final = "pv_kwh"
CONSUMPTION: Final = "consumption_minus_water_kwh"
GRID_NET_EXPORT: Final = "grid_net_export_kwh"
SOC: Final = "battery_soc_pct"


@dataclass(frozen=True)
class DayActuals:
    """Measured energy of `energy.day`, read in `tz`."""

    energy: EnergyDay
    tz: tzinfo

    @property
    def day(self) -> date:
        return self.energy.day

    def half_hours(self, series: str) -> dict[int, float]:
        """KWh per wall-clock half hour (key 0..47); incomplete ones left out."""
        return self.energy.energy_by_half_hour(series, self.tz)

    def window_buckets(self, series: str, now: datetime | None = None) -> Buckets:
        """Return the 7:00-12:30 buckets of `series`; from `now` on when given.

        From `now` on matches a now-aware forecast view: closed buckets 0, the
        in-progress one only what came after `now` (the 5-minute slot holding
        `now` in proportion), later ones whole. A missing bucket is 0.
        """
        if now is None:
            half = self.half_hours(series)
            return Buckets(
                by_bucket={
                    Bucket(h, m): half.get(h * 2 + m // 30, 0.0)
                    for h in PV_WINDOW_HOURS
                    for m in (0, 30)
                }
            )
        by_bucket = {Bucket(h, m): 0.0 for h in PV_WINDOW_HOURS for m in (0, 30)}
        for start, value in self._slots(series):
            if value is None or start.hour not in PV_WINDOW_HOURS:
                continue
            end = start + self.energy.STEP
            if end <= now:
                continue
            share = 1.0 if start >= now else (end - now) / self.energy.STEP
            bucket = Bucket.enclosing(start)
            by_bucket[bucket] += value * share
        return Buckets(by_bucket=by_bucket)

    def hourly(self, series: str, hour: int) -> float | None:
        """Sum over a wall-clock hour; None when the hour has no data at all.

        Net-billing settles per hour: an hour's import is the negative of its
        summed net balance, never the sum of its negative 5-minute slots.
        """
        values = [
            v
            for start, v in self._slots(series)
            if start.hour == hour and v is not None
        ]
        return sum(values) if values else None

    def hour_net_export(self, now: datetime) -> float:
        """Net grid export since the full hour up to `now`, never below 0 (kWh).

        The production reads the hourly meter; here the 5-minute slots stand in
        for it, the one holding `now` in proportion.
        """
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        total = 0.0
        for start, value in self._slots(GRID_NET_EXPORT):
            if value is None or start < hour_start or start >= now:
                continue
            end = start + self.energy.STEP
            total += value if end <= now else value * ((now - start) / self.energy.STEP)
        return max(0.0, total)

    def soc_near(self, at: datetime, lookback_slots: int = 6) -> float | None:
        """Mean SoC of the 5-minute slot holding `at`, else of the latest before it."""
        slots = [(s, v) for s, v in self._slots(SOC) if s <= at]
        for _start, value in reversed(slots[-lookback_slots:]):
            if value is not None:
                return value
        return None

    def soc_series(self) -> list[tuple[datetime, float | None]]:
        """(local slot start, mean SoC) over the day."""
        return self._slots(SOC)

    def soc_between(self, first: time, end: time) -> list[float]:
        """Slot means of SoC with the slot start in [first, end)."""
        return [
            v
            for s, v in self._slots(SOC)
            if v is not None and first <= s.time() and s.time() < end
        ]

    def first_soc_at_least(self, level: float) -> datetime | None:
        """Start of the first 5-minute slot whose mean SoC reaches `level`."""
        for start, value in self._slots(SOC):
            if value is not None and value >= level:
                return start
        return None

    def series_between(
        self, series: str, first: datetime, end: datetime
    ) -> list[float]:
        """Values of `series` whose slot starts in [first, end)."""
        return [v for s, v in self._slots(series) if v is not None and first <= s < end]

    def at(self, minute: int) -> datetime:
        """Return the local moment `minute` minutes after midnight (wall clock)."""
        return datetime.combine(self.day, time(0), tzinfo=self.tz) + timedelta(
            minutes=minute
        )

    def _slots(self, series: str) -> list[tuple[datetime, float | None]]:
        """(local slot start, value) in absolute-time order."""
        start_utc = self.energy.start.astimezone(UTC)
        return [
            ((start_utc + i * self.energy.STEP).astimezone(self.tz), value)
            for i, value in enumerate(self.energy.series.get(series, []))
        ]
