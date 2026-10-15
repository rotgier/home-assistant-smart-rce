"""EnergyDay — one day of measured energy in 5-minute slots, kept beyond the recorder.

HA keeps 5-minute statistics for 10 days only. Consumers that look further
back (8 previous workdays, 5 previous Saturdays/Sundays) fell back to gaps or
to hourly statistics split evenly over two half hours. An archived EnergyDay
keeps the original 5-minute resolution indefinitely; coarser views are
derived from it, never the other way round.

Slots are indexed from local midnight in absolute time (slot i starts at
`start + i * STEP`), so DST days simply have 276 or 300 slots. Consumers that
think in wall-clock half hours read `energy_by_half_hour` — the one place the
5-minute slots are folded into 30-minute ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, ClassVar, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

SCHEMA_VERSION: Final = 1

# What each series holds, written into every record so the file explains itself.
SERIES_UNITS: Final[dict[str, str]] = {
    "pv_kwh": "kWh produced in the slot",
    "consumption_kwh": "kWh consumed in the slot (whole house)",
    "consumption_minus_water_kwh": "kWh consumed in the slot without water heating",
    "grid_net_export_kwh": "kWh net to the grid in the slot: + export, - import",
    "battery_soc_pct": "battery state of charge, % mean over the slot",
}


@dataclass(frozen=True)
class EnergyDay:
    """Measured energy of one local day, per 5-minute slot (None = no data)."""

    STEP: ClassVar[timedelta] = timedelta(minutes=5)
    HALF_HOUR_MINUTES: ClassVar[int] = 30
    _KIND: ClassVar[str] = "energy_day"

    day: date
    start: datetime  # local midnight, tz-aware
    series: dict[str, list[float | None]]
    # "recorder" = written by HA from its statistics; "recorder_dump" = rebuilt
    # offline from a raw dump of the same statistics.
    source: str = "recorder"

    @classmethod
    def from_readings(
        cls,
        day: date,
        tz: tzinfo,
        readings: Mapping[str, Mapping[datetime, float | None]],
        *,
        source: str = "recorder",
    ) -> EnergyDay:
        """Lay readings (series → slot start → value) onto the day's slot grid.

        Readings outside the day are ignored; slots without a reading stay None.
        """
        start = datetime.combine(day, time(0), tzinfo=tz)
        end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)
        start_utc = start.astimezone(UTC)
        # Aware datetimes sharing a tzinfo subtract as wall clock; go via UTC
        # so a DST day gets its real 23 or 25 hours.
        count = int((end.astimezone(UTC) - start_utc) / cls.STEP)
        series: dict[str, list[float | None]] = {}
        for name, by_start in readings.items():
            slots: list[float | None] = [None] * count
            for slot_start, value in by_start.items():
                index = int((slot_start.astimezone(UTC) - start_utc) / cls.STEP)
                if 0 <= index < count and value is not None:
                    slots[index] = round(value, 4)
            series[name] = slots
        return cls(day=day, start=start, series=series, source=source)

    @property
    def slot_count(self) -> int:
        return len(next(iter(self.series.values()), []))

    def has_data(self) -> bool:
        return any(self.filled_slots(name) for name in self.series)

    def is_complete(self) -> bool:
        """Every slot of every series holds a value."""
        return all(
            self.filled_slots(name) == len(values)
            for name, values in self.series.items()
        )

    def improves_on(self, other: EnergyDay) -> bool:
        """No series has fewer values than in `other` and some series has more.

        A day archived before its last statistic was compiled (a restart just
        after midnight) is rewritten only by a copy that loses nothing.
        """
        names = set(self.series) | set(other.series)
        mine = {name: self.filled_slots(name) for name in names}
        theirs = {name: other.filled_slots(name) for name in names}
        return all(mine[n] >= theirs[n] for n in names) and mine != theirs

    def filled_slots(self, name: str) -> int:
        """How many slots of `name` hold a value — a completeness check."""
        return sum(value is not None for value in self.series.get(name, []))

    def energy_by_half_hour(self, name: str, tz: tzinfo) -> dict[int, float]:
        """KWh of an energy series per wall-clock half hour (key 0..47) in `tz`.

        `tz` is the zone, not `start`'s offset: a day read back from the
        archive carries a fixed offset, which is wrong after a DST switch.
        A half hour missing any of its 5-minute values is left out — part of
        it is not its energy. On the autumn DST day the repeated hour adds to
        the same keys; in spring the skipped hour has none.
        """
        start_utc = self.start.astimezone(UTC)
        sums: dict[int, float] = {}
        gaps: set[int] = set()
        for index, value in enumerate(self.series.get(name, [])):
            local = (start_utc + index * self.STEP).astimezone(tz)
            key = (local.hour * 60 + local.minute) // self.HALF_HOUR_MINUTES
            if value is None:
                gaps.add(key)
            else:
                sums[key] = sums.get(key, 0.0) + value
        return {key: kwh for key, kwh in sums.items() if key not in gaps}

    @classmethod
    def from_records(cls, records: Iterable[Mapping[str, Any]]) -> EnergyDay | None:
        """Return the day's energy record among the day file's records, if any."""
        for record in records:
            if record.get("kind") == cls._KIND:
                return cls.from_dict(record)
        return None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EnergyDay:
        return cls(
            day=date.fromisoformat(data["day"]),
            start=datetime.fromisoformat(data["start"]),
            series={name: list(values) for name, values in data["series"].items()},
            source=str(data.get("source", "recorder")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": self._KIND,
            "source": self.source,
            "day": self.day.isoformat(),
            "start": self.start.isoformat(),
            "step_minutes": int(self.STEP.total_seconds() // 60),
            "units": {name: SERIES_UNITS.get(name, "") for name in self.series},
            "series": self.series,
        }
