"""What the installation saved, month by month.

Two components, deliberately kept apart because they answer different questions:
self-consumption is energy never bought, the deposit is energy sold and later
used to pay for what was bought. Adding them gives the bill reduction; keeping
them separate shows which half is doing the work.

Self-consumption is valued at the FULL retail price of the zone it displaced
(energy plus variable distribution), because that is what the kWh would have
cost. The deposit only ever offsets the energy component — distribution is paid
either way — which is why the deposit half is worth less per kWh.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .tariff import Zone

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .billing_month import BillingMonth
    from .deposit_ledger import MonthSettlement
    from .tariff import Tariff


def compute_savings(
    settlements: Sequence[MonthSettlement],
    self_consumption: Mapping[BillingMonth, Mapping[Zone, float]],
    tariff: Tariff,
    legacy_pln: float,
) -> SavingsReport:
    """Value every settled month, then add the pre-measurement lump.

    `legacy_pln` covers the era before hourly household data exists (the G11
    years): its self-consumption was estimated once by the standalone calculator
    and is carried as a single figure rather than re-derived from nothing.
    """
    months = [
        _month_savings(settlement, self_consumption.get(settlement.month), tariff)
        for settlement in settlements
    ]
    return SavingsReport(months=tuple(months), legacy_pln=legacy_pln)


def _month_savings(
    settlement: MonthSettlement,
    zones: Mapping[Zone, float] | None,
    tariff: Tariff,
) -> MonthlySavings:
    rates = tariff.for_month(settlement.month)
    volumes = zones or dict.fromkeys(Zone, 0.0)
    return MonthlySavings(
        month=settlement.month,
        self_consumption_kwh=dict(volumes),
        self_consumption_pln=rates.energy_cost(volumes)
        + rates.distribution_cost(volumes),
        deposit_used_pln=settlement.used,
    )


@dataclass(frozen=True)
class MonthlySavings:
    """One month of avoided cost."""

    month: BillingMonth
    self_consumption_kwh: Mapping[Zone, float]
    self_consumption_pln: float
    deposit_used_pln: float

    @property
    def total_pln(self) -> float:
        return self.self_consumption_pln + self.deposit_used_pln

    @property
    def total_kwh(self) -> float:
        return sum(self.self_consumption_kwh.values())


@dataclass(frozen=True)
class SavingsReport:
    """Savings over the whole life of the installation."""

    months: tuple[MonthlySavings, ...]
    legacy_pln: float
    """Self-consumption before hourly household data existed — estimated once."""

    @property
    def measured_pln(self) -> float:
        """Everything derived from measured months, both components."""
        return sum(month.total_pln for month in self.months)

    @property
    def total_pln(self) -> float:
        return self.measured_pln + self.legacy_pln

    @property
    def self_consumption_pln(self) -> float:
        return (
            sum(month.self_consumption_pln for month in self.months) + self.legacy_pln
        )

    @property
    def deposit_pln(self) -> float:
        return sum(month.deposit_used_pln for month in self.months)

    def by_year(self) -> dict[int, float]:
        """Totals per calendar year. The legacy lump lands on its own years."""
        totals: dict[int, float] = defaultdict(float)
        for month in self.months:
            totals[month.month.year] += month.total_pln
        return dict(sorted(totals.items()))
