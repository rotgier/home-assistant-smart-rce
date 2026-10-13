"""EnergyDay — one day of measured energy in 5-minute slots, kept beyond the recorder.

HA keeps 5-minute statistics for 10 days only. Consumers that look further
back (8 previous workdays, 5 previous Saturdays/Sundays) fell back to gaps or
to hourly statistics split evenly over two half hours. An archived EnergyDay
keeps the original 5-minute resolution indefinitely; coarser views are
derived from it, never the other way round.

Slots are indexed from local midnight in absolute time (slot i starts at
`start + i * STEP`), so DST days simply have 276 or 300 slots.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, ClassVar, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

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

    def filled_slots(self, name: str) -> int:
        """How many slots of `name` hold a value — a completeness check."""
        return sum(value is not None for value in self.series.get(name, []))

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
