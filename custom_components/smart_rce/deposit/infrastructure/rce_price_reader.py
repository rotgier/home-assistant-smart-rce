"""HourlyPriceProvider backed by the PSE client that `ems` already owns.

Cross-context bridge (ADR-025 #3): the deposit context needs historical hourly
RCE, `ems` has a client whose endpoint accepts any `business_date`. Rather than a
second HTTP client with a second copy of the 15-minute aggregation rule, the
factory hands the existing one in and this adapter narrows it to the port. Typed
structurally, so nothing here imports `ems`.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGER = logging.getLogger(__name__)


class _DayPrices(Protocol):
    hour_price: tuple[float, ...]


class PriceSource(Protocol):
    async def async_get_prices(self, day: datetime.datetime) -> _DayPrices | None: ...


class RcePriceReader:
    """Adapts the ems PSE client to `HourlyPriceProvider`."""

    def __init__(self, source: PriceSource) -> None:
        self._source = source

    async def async_prices_for(self, day: datetime.date) -> Mapping[int, float] | None:
        """Hour-indexed prices, or None when PSE has nothing for that day."""
        prices = await self._source.async_get_prices(
            datetime.datetime.combine(day, datetime.time.min)
        )
        if prices is None or not prices.hour_price:
            _LOGGER.debug("No RCE prices published for %s", day)
            return None
        # Position equals clock hour on every day except the two DST switches,
        # where the day has 23 or 25 hours. The resulting misalignment shifts a
        # few groszy on two days a year — not worth a second parsing path.
        return dict(enumerate(prices.hour_price))
