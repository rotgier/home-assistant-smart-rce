"""HourReading — one hour as the meter reports it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class HourReading:
    """One hour of metered energy, already hourly-balanced by the meter.

    Balanced means export and import within the same hour have cancelled out —
    exactly the basis TAURON bills on, so at most one of the two is non-zero.
    """

    exported_kwh: float
    imported_kwh: float


def is_balanced(readings: Mapping[int, HourReading]) -> bool:
    """Tell a day the meter has balanced from one it has only listed.

    TAURON publishes the day's twenty-four slots before it computes them: every
    value comes back zero, which is indistinguishable from a real day of doing
    nothing. A household never goes a whole day without importing a single kWh —
    not at night, not with a fridge — so zero import across every hour is the
    tell.

    This matters because the zeros are permanent if stored: the fetch watermark
    moves past the day and nothing ever asks for it again. Measured 2026-08-27:
    every day fetched the morning after came back empty, every day fetched two
    days later came back complete.
    """
    return any(reading.imported_kwh > 0 for reading in readings.values())
