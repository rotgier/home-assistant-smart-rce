"""Cheap hours of a day — when exporting the PV pays less than storing it.

An hour from 07:00 to T2 whose gross RCE is below production's export bar
(`number.ems_export_threshold`). The night charge leaves room for the sun of
those hours (`MorningChargeFloor`); a day without any is a dear day
(user's decision 28.10.2026): the night charges to 100 and nothing is sold in
the morning. Kept outside `battery_schedule` so the forecast scoreboard can tell
a dear day without importing the aggregate's package (`live_reload` order).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from .pv_surplus import surplus_window_end

if TYPE_CHECKING:
    from datetime import date

    from .rce import RceDayPrices

FIRST_HOUR: Final = 7


def is_dear_day(day: date, prices: RceDayPrices, export_threshold: float) -> bool:
    """Tell whether no hour from 07:00 to T2 is below the export bar."""
    return not cheap_hours(day, prices, export_threshold)


def cheap_hours(day: date, prices: RceDayPrices, export_threshold: float) -> list[int]:
    """Return the hours from 07:00 to T2 below the export bar (gross PLN/MWh)."""
    return [
        hour
        for hour in range(FIRST_HOUR, surplus_window_end(day))
        if prices.gross_at(hour) < export_threshold
    ]
