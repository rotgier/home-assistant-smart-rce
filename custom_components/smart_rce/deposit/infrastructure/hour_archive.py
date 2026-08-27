"""Per-month stores holding the hourly detail of settled days.

Deliberately not a `Repository[T]`: that base owns exactly one aggregate and logs
a field-by-field diff on every save, which is right for a handful of scalars and
absurd for a month of hours. This owns a store per month instead, writes only the
months a run actually touched, and loads a month only when somebody asks for it —
boot must not read three years of history to settle yesterday.
"""

from __future__ import annotations

from collections import defaultdict
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.helpers.storage import Store

from ..domain.billing_month import BillingMonth
from ..domain.hourly_history import MonthlyHours

if TYPE_CHECKING:
    from collections.abc import Mapping
    import datetime

    from homeassistant.core import HomeAssistant

    from ..domain.hourly_history import PricedHour

_STORAGE_VERSION: Final = 1
_KEY_PREFIX: Final = "deposit_hours"

_LOGGER = logging.getLogger(__name__)


class HourArchive:
    """Reads and writes hourly history, one file per billing month."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._stores: dict[BillingMonth, Store[dict[str, Any]]] = {}

    async def async_record(
        self, days: Mapping[datetime.date, Mapping[int, PricedHour]]
    ) -> None:
        """Store these days, one write per month they fall in.

        A run re-reads the trailing week, so around the turn of a month it
        touches two — hence grouping first and saving once per month rather than
        once per day.
        """
        by_month: dict[BillingMonth, dict[datetime.date, Mapping[int, PricedHour]]] = (
            defaultdict(dict)
        )
        for day, hours in days.items():
            by_month[BillingMonth(day.year, day.month)][day] = hours
        for month, days_of_month in by_month.items():
            monthly = await self.async_month(month)
            for day, hours in days_of_month.items():
                monthly.record(day, hours)
            await self._store_for(month).async_save(monthly.to_dict())
        _LOGGER.debug(
            "HourArchive: stored %d day(s) across %d month(s)", len(days), len(by_month)
        )

    async def async_month(self, month: BillingMonth) -> MonthlyHours:
        """Load one month, or an empty one when it was never written."""
        data = await self._store_for(month).async_load()
        return MonthlyHours.from_dict(month, data or {})

    def _store_for(self, month: BillingMonth) -> Store[dict[str, Any]]:
        if month not in self._stores:
            self._stores[month] = Store(
                self._hass,
                _STORAGE_VERSION,
                f"{_KEY_PREFIX}_{month.year}_{month.month:02d}",
            )
        return self._stores[month]
