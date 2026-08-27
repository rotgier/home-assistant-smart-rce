"""RCEm — the monthly market price, kept only to compare settlement regimes.

Until June 2024 exported energy was valued at one price per month (RCEm); since
July 2024 it is valued at the price of the hour it left the house. Both regimes
apply the same statutory coefficient, so pricing the very same exported volumes
at RCEm isolates what the hourly regime is worth on its own — the premium for
choosing *when* to export, with production held constant.

RCEm cannot be derived from the hourly series we already fetch (PSE weights it
by balancing-market volumes) and PSE publishes no API for it, so it arrives two
ways: a shipped table that works offline, and whatever the OIRE page currently
says layered on top. A month without a price either way simply drops out of the
comparison.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from .billing_month import BillingMonth
from .settlement_regime import HOURLY_PRICING_FROM, deposit_coefficient

if TYPE_CHECKING:
    from collections.abc import Mapping

_PLN_PER_MWH: Final = 1000.0


class MonthlyMarketPrices:
    """RCEm per month, in PLN/MWh net."""

    def __init__(self, prices: Mapping[BillingMonth, float] | None = None) -> None:
        self._prices = dict(prices or {})

    @classmethod
    def from_dict(cls, data: Mapping[str, float]) -> MonthlyMarketPrices:
        return cls({BillingMonth.parse(month): price for month, price in data.items()})

    @property
    def by_month(self) -> dict[BillingMonth, float]:
        """The prices themselves, for layering onto another table."""
        return dict(self._prices)

    def to_dict(self) -> dict[str, float]:
        return {str(month): price for month, price in sorted(self._prices.items())}

    def merged_with(self, prices: Mapping[BillingMonth, float]) -> MonthlyMarketPrices:
        """Layer published prices over these. Newer wins — PSE corrects up to a year back."""
        return MonthlyMarketPrices({**self._prices, **prices})

    def deposit_for(self, month: BillingMonth, exported_kwh: float) -> float | None:
        """Value `exported_kwh` the way the monthly-price regime would have.

        None when there is nothing to compare: either the month predates hourly
        pricing (the monthly price was the settlement, so the two regimes are the
        same number) or no price is published yet — which is always true of the
        newest month, since PSE publishes around the 11th of the next one.
        """
        if month < HOURLY_PRICING_FROM:
            return None
        price = self._prices.get(month)
        if price is None:
            return None
        return exported_kwh * price / _PLN_PER_MWH * deposit_coefficient(month)
