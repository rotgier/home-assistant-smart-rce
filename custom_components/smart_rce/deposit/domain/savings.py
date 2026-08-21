"""What the installation saved, month by month, under two different baselines.

**Baseline A — "vs no PV, same tariff"** (`total_pln`): self-consumption valued at
the retail price of the zone it displaced, plus the deposit actually spent on the
bill. Two components kept apart because they answer different questions —
self-consumption is energy never bought, the deposit is energy sold and later used
to pay for what was bought. Self-consumption gets the FULL retail price (energy
plus variable distribution) because that is what the kWh would have cost; the
deposit only ever offsets the energy component, which is why it is worth less per
kWh.

**Baseline B — "vs no PV at all"** (`without_pv_pln` / `with_pv_pln`): what the
bill would have been without the installation, against what it actually was.
Crucially it prices the counterfactual on the **flat G11 tariff**, because without
PV and a battery there would be no reason to sit on a zoned tariff — G13 only pays
off when consumption can be pushed into the night. Comparing against G13 would
quietly credit the installation with the tariff switch it enabled.

B is the number to use for payback; A is the one that answers "how much smaller is
my bill than it would be". B is only available for months with measured
self-consumption — before that the household had no hourly data and was on G11
anyway, so the two baselines coincide there.
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
    imports: Mapping[BillingMonth, Mapping[Zone, float]],
    self_consumption: Mapping[BillingMonth, Mapping[Zone, float]],
    tariff: Tariff,
    legacy: LegacyEra,
) -> SavingsReport:
    """Value every settled month, then add the pre-measurement lump.

    `legacy` covers the era before hourly household data exists (the G11 years):
    estimated once by the standalone calculator and carried as fixed figures
    rather than re-derived from data Home Assistant never had.
    """
    months = [
        _month_savings(
            settlement,
            imports.get(settlement.month),
            self_consumption.get(settlement.month),
            tariff,
        )
        for settlement in settlements
    ]
    return SavingsReport(months=tuple(months), legacy=legacy)


def _month_savings(
    settlement: MonthSettlement,
    imported: Mapping[Zone, float] | None,
    self_consumed: Mapping[Zone, float] | None,
    tariff: Tariff,
) -> MonthlySavings:
    rates = tariff.for_month(settlement.month)
    volumes = self_consumed or dict.fromkeys(Zone, 0.0)
    imported = imported or dict.fromkeys(Zone, 0.0)
    paid = rates.energy_cost(imported) + rates.distribution_cost(imported)
    return MonthlySavings(
        month=settlement.month,
        self_consumption_kwh=dict(volumes),
        self_consumption_pln=rates.energy_cost(volumes)
        + rates.distribution_cost(volumes),
        deposit_used_pln=settlement.used,
        import_kwh=dict(imported),
        paid_variable_pln=paid - settlement.used,
        without_pv_pln=_counterfactual(settlement.month, volumes, imported, tariff),
    )


def _counterfactual(
    month: BillingMonth,
    self_consumed: Mapping[Zone, float],
    imported: Mapping[Zone, float],
    tariff: Tariff,
) -> float | None:
    """Price the whole month's consumption on the flat tariff, gross PLN.

    None when self-consumption was not measured — the household total is then
    unknown, and guessing it would put a made-up number next to real ones.
    """
    flat = tariff.flat_for_month(month)
    measured = sum(self_consumed.values())
    if flat is None or measured <= 0:
        return None
    return flat.cost(measured + sum(imported.values()))


@dataclass(frozen=True)
class MonthlySavings:
    """One month of avoided cost, under both baselines."""

    month: BillingMonth
    self_consumption_kwh: Mapping[Zone, float]
    self_consumption_pln: float
    deposit_used_pln: float
    import_kwh: Mapping[Zone, float]
    paid_variable_pln: float
    """What the variable part of the bill actually came to, after the deposit."""
    without_pv_pln: float | None
    """Same consumption on the flat G11 tariff, or None when not measurable."""

    @property
    def total_pln(self) -> float:
        """Baseline A: bill reduction against the same tariff."""
        return self.self_consumption_pln + self.deposit_used_pln

    @property
    def avoided_pln(self) -> float | None:
        """Baseline B: what having the installation actually spared this month."""
        if self.without_pv_pln is None:
            return None
        return self.without_pv_pln - self.paid_variable_pln

    @property
    def total_kwh(self) -> float:
        return sum(self.self_consumption_kwh.values())

    @property
    def consumption_kwh(self) -> float:
        return self.total_kwh + sum(self.import_kwh.values())


@dataclass(frozen=True)
class LegacyEra:
    """The era before hourly household data, carried as fixed totals.

    That era ran on the flat G11 tariff, so both baselines coincide there and the
    counterfactual is as solid as the measured months — it simply cannot be
    recomputed, because the inputs were never recorded.
    """

    self_consumption_pln: float
    without_pv_pln: float
    paid_pln: float

    @property
    def avoided_pln(self) -> float:
        return self.without_pv_pln - self.paid_pln


@dataclass(frozen=True)
class SavingsReport:
    """Savings over the whole life of the installation."""

    months: tuple[MonthlySavings, ...]
    legacy: LegacyEra

    @property
    def measured_pln(self) -> float:
        """Everything derived from measured months, both components."""
        return sum(month.total_pln for month in self.months)

    @property
    def legacy_pln(self) -> float:
        return self.legacy.self_consumption_pln

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

    @property
    def counterfactual_months(self) -> tuple[MonthlySavings, ...]:
        """Return the months where the "without PV" comparison can be made."""
        return tuple(m for m in self.months if m.without_pv_pln is not None)

    @property
    def without_pv_pln(self) -> float:
        """Lifetime counterfactual bill: measured months plus the legacy era."""
        measured = sum(m.without_pv_pln or 0.0 for m in self.counterfactual_months)
        return measured + self.legacy.without_pv_pln

    @property
    def paid_pln(self) -> float:
        measured = sum(m.paid_variable_pln for m in self.counterfactual_months)
        return measured + self.legacy.paid_pln

    @property
    def avoided_pln(self) -> float:
        return self.without_pv_pln - self.paid_pln

    def by_year(self) -> dict[int, float]:
        """Baseline A totals per calendar year."""
        totals: dict[int, float] = defaultdict(float)
        for month in self.months:
            totals[month.month.year] += month.total_pln
        return dict(sorted(totals.items()))
