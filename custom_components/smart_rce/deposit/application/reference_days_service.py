"""Fills the rest of the current month with the same dates a year ago.

The reference year needs whole months, but the current one is half-written. The
days that have happened come from the ledger; this supplies the days that have
not, taken from the hourly archive exactly as they were measured a year earlier.

Why the actual days rather than a scaled month total: within a single month the
shape is set by weather, not by the calendar. Measured here — October 2024 put
63% of its export in the second half, November 2025 put 76% in the first. No
average reproduces that.

The days are re-dated to this year before they are valued, so tariff zones and
the deposit coefficient come from the calendar they will stand in for. Without
that, a Saturday a year ago (all day in the cheap night zone) would keep its
zones against a Sunday now, and the weekends would drift a day or two each year.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

from ..domain.billing_month import BillingMonth
from ..domain.day_valuation import value_day
from ..domain.meter_reading import HourReading

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..domain.hourly_history import PricedHour
    from ..domain.settlement_history import DayRecord
    from ..infrastructure.hour_archive import HourArchive
    from .deposit_service import DepositService

_MONTHS_IN_YEAR = 12

_LOGGER = logging.getLogger(__name__)


class ReferenceDaysService:
    """Hands the deposit report last year's version of this month's remaining days."""

    def __init__(self, archive: HourArchive, deposit: DepositService) -> None:
        self._archive = archive
        self._deposit = deposit

    async def async_refresh(self, today: datetime.date) -> None:
        """Load the same calendar month a year ago and re-date it onto this one."""
        month = BillingMonth(today.year, today.month)
        a_year_ago = month.shifted(-_MONTHS_IN_YEAR)
        stored = await self._archive.async_month(a_year_ago)
        days = self._re_dated(stored.days, month)
        if not days:
            _LOGGER.debug("ReferenceDays: nothing archived for %s", a_year_ago)
            return
        self._deposit.update_last_year_days(days)
        _LOGGER.debug(
            "ReferenceDays: %d day(s) of %s standing in for %s",
            len(days),
            a_year_ago,
            month,
        )

    @staticmethod
    def _re_dated(
        archived: Mapping[datetime.date, Mapping[int, PricedHour]],
        month: BillingMonth,
    ) -> dict[datetime.date, DayRecord]:
        """Value each archived day as if it fell on the same date this year.

        A day that does not exist this year is dropped — 29 February only has a
        counterpart every fourth year.
        """
        days: dict[datetime.date, DayRecord] = {}
        for day, hours in archived.items():
            try:
                target = datetime.date(month.year, month.month, day.day)
            except ValueError:
                continue
            readings = {
                hour: HourReading(
                    exported_kwh=priced.exported_kwh,
                    imported_kwh=priced.imported_kwh,
                )
                for hour, priced in hours.items()
            }
            prices = {
                hour: priced.price_pln_mwh
                for hour, priced in hours.items()
                if priced.price_pln_mwh is not None
            }
            days[target] = value_day(target, readings, prices)
        return days
