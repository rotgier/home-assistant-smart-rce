"""Tariff — what an imported kWh costs, per G13 zone.

Two cost components are tracked separately because they behave differently in
net-billing: the deposit offsets **active energy only**, while distribution is
always paid in cash. That split is also what sets the break-even RCE for the
"feed at peak, re-buy at night" strategy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .billing_month import BillingMonth

VAT: Final = 1.23


class Zone(StrEnum):
    """G13 tariff zones."""

    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


@dataclass(frozen=True)
class ZoneRates:
    """Net PLN/kWh for one month: active energy + variable distribution."""

    energy: Mapping[Zone, float]
    distribution: Mapping[Zone, float]

    def energy_cost(self, kwh: Mapping[Zone, float]) -> float:
        """Gross PLN for active energy — the part the deposit can offset."""
        return sum(kwh.get(z, 0.0) * self.energy[z] for z in Zone) * VAT

    def distribution_cost(self, kwh: Mapping[Zone, float]) -> float:
        """Gross PLN for variable distribution — always cash, never the deposit."""
        return sum(kwh.get(z, 0.0) * self.distribution[z] for z in Zone) * VAT

    @property
    def night_marginal_cost(self) -> float:
        """Net PLN/kWh of a kWh re-bought at night (T3) — energy + distribution.

        This is the break-even RCE for exporting a stored kWh instead of keeping
        it: `gain = VAT * (RCE - night_marginal_cost)`, so the VAT factor cancels
        and the threshold is a plain price comparison.
        """
        return self.energy[Zone.T3] + self.distribution[Zone.T3]


class Tariff:
    """Rates over time. Months outside the known range clamp to the nearest edge.

    Clamping is deliberate: the current month is billed before its invoice
    arrives, and projections run past the last published tariff. Both must use
    the closest known rates rather than fail.
    """

    def __init__(self, rates: Mapping[BillingMonth, ZoneRates]) -> None:
        if not rates:
            raise ValueError("Tariff needs at least one month of rates")
        self._rates = dict(rates)
        self._first = min(self._rates)
        self._last = max(self._rates)

    @property
    def latest(self) -> ZoneRates:
        """Rates of the most recent known month — the basis for projections."""
        return self._rates[self._last]

    def for_month(self, month: BillingMonth) -> ZoneRates:
        return self._rates[min(max(month, self._first), self._last)]
