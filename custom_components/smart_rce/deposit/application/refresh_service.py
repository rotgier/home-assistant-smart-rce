"""DepositRefreshService — pulls newly closed days and rebuilds the report.

Runs once a day. Everything it needs to be safe to re-run is in the watermark:
it asks the meter only for days after `last_data_day`, so a failed or skipped run
costs nothing but a later catch-up.

Two guards worth knowing about:
- **one login per run.** TAURON bans on a burst of logins (~12 h), so the whole
  day range goes in a single call to the meter adapter, never day by day.
- **stop at the first day without prices** instead of skipping it. Skipping would
  advance the watermark past a day that was never valued, losing it silently;
  stopping just leaves it for the next run.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Final

from ..domain.day_valuation import value_day
from ..domain.settlement_history import DayRecord

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ..domain.meter_reading import HourReading
    from ..infrastructure.history_repository import HistoryRepository
    from .ports import HourlyPriceProvider, MeterReadingsProvider

_LOGGER = logging.getLogger(__name__)


class DepositRefreshService:
    """Keeps the settlement history current."""

    # After a long outage, catch up in chunks rather than asking TAURON for a
    # year in one go. Successive runs walk forward until the watermark is current.
    _MAX_DAYS_PER_RUN: Final = 45

    def __init__(
        self,
        repository: HistoryRepository,
        prices: HourlyPriceProvider,
        meter: MeterReadingsProvider,
        on_updated: Callable[[], None],
    ) -> None:
        self._repository = repository
        self._prices = prices
        self._meter = meter
        self._on_updated = on_updated

    async def async_refresh(self, today: datetime.date) -> int:
        """Fetch and value everything closed since the watermark. Returns days added."""
        window = self._window(today)
        if window is None:
            _LOGGER.debug("Deposit refresh: nothing new to fetch")
            return 0
        start, end = window
        readings = await self._meter.async_readings_for(start, end)
        records = await self._value(readings)
        if not records:
            _LOGGER.warning(
                "Deposit refresh: %s..%s returned no usable days", start, end
            )
            return 0
        self._repository.history.add_days(records)
        await self._repository.persist()
        self._on_updated()
        _LOGGER.info(
            "Deposit refresh: added %d day(s), watermark now %s",
            len(records),
            self._repository.history.last_data_day,
        )
        return len(records)

    def _window(
        self, today: datetime.date
    ) -> tuple[datetime.date, datetime.date] | None:
        """Return the closed days still missing, capped to one run's worth."""
        start = self._repository.history.next_day_to_fetch()
        end = today - datetime.timedelta(days=1)
        if start is None or start > end:
            return None
        return start, min(
            end, start + datetime.timedelta(days=self._MAX_DAYS_PER_RUN - 1)
        )

    async def _value(
        self, readings: Mapping[datetime.date, Mapping[int, HourReading]]
    ) -> list[DayRecord]:
        """Value days in order, stopping at the first one PSE has no prices for."""
        records: list[DayRecord] = []
        for day in sorted(readings):
            prices = await self._prices.async_prices_for(day)
            if prices is None:
                _LOGGER.warning(
                    "Deposit refresh: no RCE prices for %s — stopping here, "
                    "the day will be retried on the next run",
                    day,
                )
                break
            records.append(value_day(day, readings[day], prices))
        return records
