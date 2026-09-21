"""Rates over time — the settlement view of the tariff.

`Zone` and `ZoneRates` live in the shared `tariff` package because both bounded
contexts read them. What stays here is what only settlement needs: rates keyed
by billing month, and the counterfactual flat tariff used for comparison.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ...tariff import VAT, ZoneRates
from .billing_month import BillingMonth


@dataclass(frozen=True)
class FlatRates:
    """Net PLN/kWh of a single-rate tariff — the counterfactual G11.

    Kept apart from `ZoneRates` on purpose: a flat tariff has no zones, and
    pretending otherwise (three identical entries) would invite code that reads
    a zone where none exists.
    """

    energy: float
    distribution: float

    def cost(self, kwh: float) -> float:
        """Gross PLN for `kwh` bought on this tariff."""
        return kwh * (self.energy + self.distribution) * VAT


class Tariff:
    """Rates over time. Months outside the known range clamp to the nearest edge.

    Clamping is deliberate: the current month is billed before its invoice
    arrives, and projections run past the last published tariff. Both must use
    the closest known rates rather than fail.
    """

    def __init__(
        self,
        rates: Mapping[BillingMonth, ZoneRates],
        flat: Mapping[BillingMonth, FlatRates] | None = None,
    ) -> None:
        if not rates:
            raise ValueError("Tariff needs at least one month of rates")
        self._rates = dict(rates)
        self._flat = dict(flat or {})
        self._first = min(self._rates)
        self._last = max(self._rates)

    @property
    def latest(self) -> ZoneRates:
        """Rates of the most recent known month — the basis for projections."""
        return self._rates[self._last]

    def for_month(self, month: BillingMonth) -> ZoneRates:
        return self._rates[self._clamp(month)]

    def flat_for_month(self, month: BillingMonth) -> FlatRates | None:
        """Counterfactual single-rate tariff, or None when it is not known."""
        return self._flat.get(self._clamp(month))

    def _clamp(self, month: BillingMonth) -> BillingMonth:
        return min(max(month, self._first), self._last)
