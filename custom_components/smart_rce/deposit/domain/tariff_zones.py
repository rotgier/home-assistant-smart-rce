"""Mapping a moment in time to its G13 tariff zone.

Needed because the meter reports hourly energy while the bill is priced per zone.
Schedule verified identical across the 2024/2025/2026 TAURON Dystrybucja tariffs;
weekends and statutory holidays are T3 all day in every year.

    summer (1 Apr - 30 Sep):  T1 07-13 | T2 19-22 | T3 rest
    winter (1 Oct - 31 Mar):  T1 07-13 | T2 16-21 | T3 rest
"""

from __future__ import annotations

import datetime
from typing import Final

from .tariff import Zone

_SUMMER_MONTHS: Final = range(4, 10)  # April through September
_MORNING_PEAK: Final = range(7, 13)
_SUMMER_EVENING_PEAK: Final = range(19, 22)
_WINTER_EVENING_PEAK: Final = range(16, 21)


def zone_for(moment: datetime.datetime) -> Zone:
    """Zone of the hour starting at `moment` (local time)."""
    if moment.weekday() >= 5 or is_holiday(moment.date()):
        return Zone.T3
    if moment.hour in _MORNING_PEAK:
        return Zone.T1
    evening = (
        _SUMMER_EVENING_PEAK if moment.month in _SUMMER_MONTHS else _WINTER_EVENING_PEAK
    )
    return Zone.T2 if moment.hour in evening else Zone.T3


def is_holiday(day: datetime.date) -> bool:
    return day in _holidays(day.year)


_HOLIDAY_CACHE: dict[int, frozenset[datetime.date]] = {}


def _holidays(year: int) -> frozenset[datetime.date]:
    """Polish statutory holidays for `year`.

    Easter Sunday and Pentecost fall on Sundays, which are already T3, so only
    Easter Monday and Corpus Christi need deriving.
    """
    if year not in _HOLIDAY_CACHE:
        easter_day = easter(year)
        _HOLIDAY_CACHE[year] = frozenset(
            {
                datetime.date(year, 1, 1),
                datetime.date(year, 1, 6),
                datetime.date(year, 5, 1),
                datetime.date(year, 5, 3),
                datetime.date(year, 8, 15),
                datetime.date(year, 11, 1),
                datetime.date(year, 11, 11),
                datetime.date(year, 12, 25),
                datetime.date(year, 12, 26),
                easter_day + datetime.timedelta(days=1),
                easter_day + datetime.timedelta(days=60),
            }
        )
    return _HOLIDAY_CACHE[year]


def easter(year: int) -> datetime.date:
    """Gregorian Easter Sunday (Meeus/Jones/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return datetime.date(year, month, day)
