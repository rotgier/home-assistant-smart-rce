"""MarketPriceService — keeps the published RCEm in the report up to date.

The shipped table stays the baseline: it comes from the invoice-reconciled
calculator and works offline. On top of it sits whatever was scraped, kept in its
own store so a restart does not undo it. What PSE adds matters twice a month at
most — a new price around the 11th, and the occasional correction of an older one.

Failure is not an error condition here. PSE offers no API for RCEm, so this
scrapes a page; when that breaks, the report falls back to the shipped table and
the newest months simply stay out of the regime comparison.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..infrastructure.market_price_repository import MarketPriceRepository
    from ..infrastructure.rcem_reader import PseRcemReader
    from .deposit_service import DepositService

_LOGGER = logging.getLogger(__name__)


class MarketPriceService:
    """Feeds published monthly market prices into the deposit report."""

    def __init__(
        self,
        reader: PseRcemReader,
        deposit: DepositService,
        repository: MarketPriceRepository,
    ) -> None:
        self._reader = reader
        self._deposit = deposit
        self._repository = repository

    async def async_refresh(self) -> None:
        """Fetch the published prices and hand them to the report.

        Runs on every daily pass rather than only when a price is missing: a
        correction can land on a month that already has one, and one GET a day
        of a static page is not worth a cleverer rule.
        """
        prices = await self._reader.async_prices()
        if not prices:
            return
        await self._repository.async_merge(prices)
        self._deposit.update_market_prices(self._repository.prices.by_month)
        _LOGGER.debug("MarketPriceService: %d months published", len(prices))
