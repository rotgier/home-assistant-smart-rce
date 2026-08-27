"""DepositRefreshService — pulls newly closed days and rebuilds the report.

Runs once a day. Everything it needs to be safe to re-run is in the watermark:
it asks the meter only for days after `last_data_day`, so a failed or skipped run
costs nothing but a later catch-up.

Three guards worth knowing about:
- **one login per run.** TAURON bans on a burst of logins (~12 h), so the whole
  day range goes in a single call to the meter adapter, never day by day.
- **stop at the first day that cannot be settled** instead of skipping it — a day
  with no published prices, or one the meter has not balanced yet. Skipping would
  advance the watermark past a day that was never valued, losing it silently;
  stopping just leaves it for the next run.
- **re-fetch a trailing week** on every run, not only the days after the
  watermark. TAURON fills a day in over the following days, so the first answer
  is not the final one; re-valuing the recent past lets a late correction land
  by itself. It is the same single login, a slightly wider date range — but it
  does mean there is always something to fetch, so the run **rations itself**:
  once a day when yesterday is already in, and no more than every few hours while
  it is still missing. Otherwise a debugging session with a few reloads would be
  a few logins, which is how you earn a ban.

The last two exist because of what the store looked like on 2026-08-27: four of
the previous six days sat there as zeros, each one fetched the morning after,
each one complete at the meter by the time anybody looked.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Final

from ..domain.day_valuation import value_day
from ..domain.hourly_history import PricedHour
from ..domain.meter_reading import is_balanced
from ..domain.settlement_history import DayRecord

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ..domain.meter_reading import HourReading
    from ..infrastructure.history_repository import HistoryRepository
    from .ports import HourArchive, HourlyPriceProvider, MeterReadingsProvider

_LOGGER = logging.getLogger(__name__)


class DepositRefreshService:
    """Keeps the settlement history current."""

    # After a long outage, catch up in chunks rather than asking TAURON for a
    # year in one go. Successive runs walk forward until the watermark is current.
    _MAX_DAYS_PER_RUN: Final = 45
    # How far back to re-read every run. A week covers the observed fill-in lag
    # several times over and still fits in the same request.
    _TRAILING_DAYS: Final = 7
    # While yesterday is still missing the run may try again later the same day —
    # the meter fills a day in at an hour nobody publishes. Spaced out enough that
    # even a run every hour could not turn into a burst of logins.
    _RETRY_AFTER: Final = datetime.timedelta(hours=4)

    def __init__(
        self,
        repository: HistoryRepository,
        prices: HourlyPriceProvider,
        meter: MeterReadingsProvider,
        archive: HourArchive,
        on_updated: Callable[[], None],
    ) -> None:
        self._repository = repository
        self._prices = prices
        self._meter = meter
        self._archive = archive
        self._on_updated = on_updated

    async def async_refresh(self, now: datetime.datetime) -> int:
        """Fetch and value the missing days plus the trailing week. Returns days added."""
        history = self._repository.history
        if not self._may_read_meter(now):
            _LOGGER.debug("Deposit refresh: meter read recently enough")
            return 0
        today = now.date()
        window = self._window(today)
        if window is None:
            _LOGGER.debug("Deposit refresh: nothing to fetch")
            return 0
        start, end = window
        watermark = history.last_data_day
        readings = await self._meter.async_readings_for(start, end)
        history.mark_meter_called(now)
        records, hours = await self._value(readings, watermark)
        await self._archive.async_record(hours)
        if not records:
            _LOGGER.warning(
                "Deposit refresh: %s..%s returned no usable days", start, end
            )
            await self._repository.persist()  # keep the meter-call mark
            return 0
        history.add_days(records)
        await self._repository.persist()
        self._on_updated()
        _LOGGER.info(
            "Deposit refresh: settled %d day(s) of %s..%s, watermark now %s",
            len(records),
            start,
            end,
            history.last_data_day,
        )
        return len(records)

    def _may_read_meter(self, now: datetime.datetime) -> bool:
        """Say whether another meter call is due.

        Once a day is plenty when yesterday is already settled — the extra reads
        only pick up late corrections. While yesterday is missing the answer is
        worth chasing sooner, because the meter publishes it at an hour nobody
        states and waiting a full day doubles the lag.
        """
        history = self._repository.history
        last = history.last_meter_call
        if last is None:
            return True
        yesterday = now.date() - datetime.timedelta(days=1)
        settled = history.last_data_day
        if settled is not None and settled >= yesterday:
            return last.date() != now.date()
        return now - last >= self._RETRY_AFTER

    def _window(
        self, today: datetime.date
    ) -> tuple[datetime.date, datetime.date] | None:
        """Return the days to read: everything missing, plus the trailing week."""
        next_day = self._repository.history.next_day_to_fetch()
        end = today - datetime.timedelta(days=1)
        if next_day is None:
            return None
        start = min(next_day, end - datetime.timedelta(days=self._TRAILING_DAYS - 1))
        if start > end:
            return None
        return start, min(
            end, start + datetime.timedelta(days=self._MAX_DAYS_PER_RUN - 1)
        )

    async def _value(
        self,
        readings: Mapping[datetime.date, Mapping[int, HourReading]],
        watermark: datetime.date | None,
    ) -> tuple[list[DayRecord], dict[datetime.date, dict[int, PricedHour]]]:
        """Value days in order, refusing to settle one that is not ready yet.

        Returns the settled days and the hourly detail behind them — the same
        pass, because the hours are in hand here and asking for them again would
        mean another login.
        """
        records: list[DayRecord] = []
        detail: dict[datetime.date, dict[int, PricedHour]] = {}
        for day in sorted(readings):
            hours = readings[day]
            if not is_balanced(hours):
                if self._may_wait(day, watermark, "is not balanced at the meter yet"):
                    continue
                break
            prices = await self._prices.async_prices_for(day)
            if prices is None:
                if self._may_wait(day, watermark, "has no published RCE prices"):
                    continue
                break
            records.append(value_day(day, hours, prices))
            detail[day] = {
                hour: PricedHour(
                    exported_kwh=reading.exported_kwh,
                    imported_kwh=reading.imported_kwh,
                    price_pln_mwh=prices.get(hour),
                )
                for hour, reading in hours.items()
            }
        return records, detail

    def _may_wait(
        self, day: datetime.date, watermark: datetime.date | None, reason: str
    ) -> bool:
        """Say whether an unsettleable day can simply be left for later.

        Yes when the watermark has already passed it: this was a re-read, and the
        stored version stays untouched. No when it is new — settling anything
        after it would move the watermark past a day nothing would ever fetch
        again, which is exactly how a week of zeros got frozen in.
        """
        if watermark is not None and day <= watermark:
            _LOGGER.debug(
                "Deposit refresh: re-read of %s %s, keeping the stored day", day, reason
            )
            return True
        _LOGGER.warning(
            "Deposit refresh: %s %s — stopping here, the day will be retried "
            "on the next run",
            day,
            reason,
        )
        return False
