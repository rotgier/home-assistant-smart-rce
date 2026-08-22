"""RCEm — the monthly market price, kept only to compare settlement regimes.

Until June 2024 exported energy was valued at one price per month (RCEm); since
July 2024 it is valued at the price of the hour it left the house. Both regimes
apply the same statutory coefficient, so pricing the very same exported volumes
at RCEm isolates what the hourly regime is worth on its own — the premium for
choosing *when* to export, with production held constant.

RCEm cannot be derived from the hourly series we already fetch (PSE weights it
by balancing-market volumes), so it is a shipped table refreshed by hand along
with the tariff. A month without a price simply drops out of the comparison.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from .settlement_regime import HOURLY_PRICING_FROM, deposit_coefficient

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .billing_month import BillingMonth

_PLN_PER_MWH: Final = 1000.0


class MonthlyMarketPrices:
    """RCEm per month, in PLN/MWh net."""

    def __init__(self, prices: Mapping[BillingMonth, float] | None = None) -> None:
        self._prices = dict(prices or {})

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
