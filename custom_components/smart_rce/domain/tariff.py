"""G13 tariff zones + marginal cost of drawing 1 kWh from the grid.

Two price systems govern every evening decision and they do not align:
RCE decides what an exported kWh earns, while the G13 zone decides what the
kWh we would later have to buy costs us. Discharging early into a cheap RCE
hour can therefore lose money if it forces a purchase in an expensive zone.

All prices here are GROSS PLN/MWh. Gross is the unit the user reasons in, and
the deposit multiplier (1.23) cancels out when comparing an export price with
a purchase cost — both sides carry it — so a gross-to-gross comparison is the
honest one. Figures are the 2026 tariff, energy + variable distribution, taken
from `tauron_faktury/ZASADY_ROZLICZENIA.md` in the fotowoltaika repo.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Final


class G13Zone(StrEnum):
    """Distribution zone of a given hour. Cost is what BUYING that hour costs."""

    T1 = "T1"
    """Morning peak, 07-13 on workdays."""

    T2 = "T2"
    """Evening peak — the expensive one. Summer 19-22, winter 16-21, workdays."""

    T3 = "T3"
    """Everything else, plus all of every weekend and public holiday."""

    @property
    def marginal_cost(self) -> float:
        """Gross PLN/MWh it costs to draw 1 kWh from the grid in this zone."""
        return _MARGINAL_COST[self]

    @classmethod
    def at(cls, moment: datetime, *, is_workday: bool) -> G13Zone:
        """Zone of `moment`. Non-workdays are T3 around the clock."""
        if not is_workday:
            return cls.T3
        hour = moment.hour
        if _T1_START <= hour < _T1_END:
            return cls.T1
        start, end = cls.evening_peak_hours(moment.date())
        if start <= hour < end:
            return cls.T2
        return cls.T3

    @staticmethod
    def evening_peak_hours(day: date) -> tuple[int, int]:
        """Half-open [start, end) of the T2 evening peak, by season.

        Summer (1 Apr - 30 Sep) 19-22; winter (1 Oct - 31 Mar) 16-21. The
        switchover moves the expensive block three hours earlier and makes it
        an hour longer, which reshapes the whole evening plan.
        """
        if _SUMMER_FIRST_MONTH <= day.month <= _SUMMER_LAST_MONTH:
            return _T2_SUMMER
        return _T2_WINTER


_T1_START: Final[int] = 7
_T1_END: Final[int] = 13
_T2_SUMMER: Final[tuple[int, int]] = (19, 22)
_T2_WINTER: Final[tuple[int, int]] = (16, 21)
_SUMMER_FIRST_MONTH: Final[int] = 4
_SUMMER_LAST_MONTH: Final[int] = 9

# Gross PLN/MWh, 2026 tariff: retail energy + variable distribution.
_MARGINAL_COST: Final[dict[G13Zone, float]] = {
    G13Zone.T1: 905.0,
    G13Zone.T2: 1496.0,
    G13Zone.T3: 626.0,
}
