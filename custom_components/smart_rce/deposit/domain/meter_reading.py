"""HourReading — one hour as the meter reports it."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HourReading:
    """One hour of metered energy, already hourly-balanced by the meter.

    Balanced means export and import within the same hour have cancelled out —
    exactly the basis TAURON bills on, so at most one of the two is non-zero.
    """

    exported_kwh: float
    imported_kwh: float
