"""Persists the RCEm prices scraped from PSE.

The shipped table in `tariff_table.json` is a floor, not a cache: it only knows
what was true when the release was cut. Without a store, every restart drops back
to it until the scrape succeeds — and if PSE is unreachable, stays there. The
prices are a handful of numbers per year, so keeping them costs nothing and the
report stops depending on someone else's website being up at boot.

Merged rather than replaced on write: a partial parse must never delete months
that were read correctly last time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from ...infrastructure.repository import Repository
from ..domain.market_price import MonthlyMarketPrices

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

    from ...infrastructure.async_task_runner import AsyncTaskRunner
    from ..domain.billing_month import BillingMonth


class MarketPriceRepository(Repository[MonthlyMarketPrices]):
    """Owns the published market prices."""

    STORAGE_KEY: ClassVar[str] = "deposit_market_prices"

    def __init__(self, hass: HomeAssistant, tasks: AsyncTaskRunner) -> None:
        super().__init__(hass, tasks)
        self._prices = MonthlyMarketPrices()

    @property
    def prices(self) -> MonthlyMarketPrices:
        return self._prices

    async def async_restore(self) -> None:
        """Load what was published as of the last successful scrape."""
        data: dict[str, Any] | None = await self._store.async_load()
        self._prices = MonthlyMarketPrices.from_dict(data or {})
        # Remember what disk already holds: the scrape returns the same table
        # every day, and without this each run would rewrite it and log the whole
        # thing as a change.
        self._last_saved = self._prices.to_dict() if data else None

    async def async_merge(self, prices: Mapping[BillingMonth, float]) -> None:
        """Fold freshly scraped prices in and persist."""
        self._prices = self._prices.merged_with(prices)
        await self.persist()

    def _get_aggregate(self) -> MonthlyMarketPrices:
        return self._prices
