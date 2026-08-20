"""ConsumptionCapacity — how much deposit the household can actually spend.

The deposit only ever offsets active energy, so the amount consumable in twelve
months is the twelve-month active-energy bill. That figure is the denominator of
the whole strategy question: while the yearly peak balance stays below it, FIFO
drains every tranche before it turns twelve months old and each exported kWh is
worth its full value. Above it, tranches start expiring at a partial cash refund
and exporting aggressively begins to destroy value.

Never hardcode it — it moves with consumption and with the tariff, and both
change faster than intuition about them.
"""

from __future__ import annotations

from dataclasses import dataclass

from .reference_year import ReferenceYear
from .tariff import ZoneRates


@dataclass(frozen=True)
class ConsumptionCapacity:
    """Twelve-month active-energy bill, gross PLN."""

    value: float

    @classmethod
    def from_reference_year(
        cls,
        reference_year: ReferenceYear,
        rates: ZoneRates,
        *,
        consumption_factor: float = 1.0,
    ) -> ConsumptionCapacity:
        """Price the realised twelve-month import profile at the given rates."""
        return cls(
            sum(rates.energy_cost(record.import_kwh) for record in reference_year)
            * consumption_factor
        )

    def utilization(self, balance: float) -> float:
        """Balance as a percentage of capacity. 100% is where forfeiting starts."""
        if self.value <= 0:
            return 0.0
        return 100.0 * balance / self.value
