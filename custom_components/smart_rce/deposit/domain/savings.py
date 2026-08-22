"""What the installation saved, month by month, under two different baselines.

**Baseline A — "vs no PV, same tariff"** (`total_pln`): self-consumption valued at
the retail price of the zone it displaced, plus the deposit actually spent on the
bill. Two components kept apart because they answer different questions —
self-consumption is energy never bought, the deposit is energy sold and later used
to pay for what was bought. Self-consumption gets the FULL retail price (energy
plus variable distribution) because that is what the kWh would have cost; the
deposit only ever offsets the energy component, which is why it is worth less per
kWh.

**Baseline B — "vs no PV at all"** (`without_pv_pln` / `paid_variable_pln`): what the
bill would have been without the installation, against what it actually was.
Crucially it prices the counterfactual on the **flat G11 tariff**, because without
PV and a battery there would be no reason to sit on a zoned tariff — G13 only pays
off when consumption can be pushed into the night. Comparing against G13 would
quietly credit the installation with the tariff switch it enabled.

B is the number to use for payback; A is the one that answers "how much smaller is
my bill than it would be".

Months from before hourly household data arrive as `LegacyMonth` — figures carried
over from the standalone calculator rather than recomputed, because Home Assistant
never recorded the inputs. That era ran on flat G11, so both baselines coincide
there and W2 equals W3 by construction.
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
    legacy: Mapping[BillingMonth, LegacyMonth],
) -> SavingsReport:
    """Value every settled month, then add the pre-measurement lump.

    Months present in `legacy` take their figures from there; the rest are derived
    from measured volumes.
    """
    months = [
        _month_savings(
            settlement,
            imports.get(settlement.month),
            self_consumption.get(settlement.month),
            tariff,
            legacy.get(settlement.month),
        )
        for settlement in settlements
    ]
    return SavingsReport(months=tuple(months))


def _month_savings(
    settlement: MonthSettlement,
    imported: Mapping[Zone, float] | None,
    self_consumed: Mapping[Zone, float] | None,
    tariff: Tariff,
    legacy: LegacyMonth | None,
) -> MonthlySavings:
    if legacy is not None:
        return _from_legacy(settlement, legacy)
    rates = tariff.for_month(settlement.month)
    volumes = self_consumed or dict.fromkeys(Zone, 0.0)
    imported = imported or dict.fromkeys(Zone, 0.0)
    measured = sum(volumes.values())
    flat = tariff.flat_for_month(settlement.month)
    paid = rates.energy_cost(imported) + rates.distribution_cost(imported)
    return MonthlySavings(
        month=settlement.month,
        self_consumption_kwh=measured,
        self_consumption_pln=rates.energy_cost(volumes)
        + rates.distribution_cost(volumes),
        self_consumption_g11_pln=None
        if flat is None or measured <= 0
        else flat.cost(measured),
        deposit_used_pln=settlement.used,
        import_kwh=sum(imported.values()),
        paid_variable_pln=paid - settlement.used,
        without_pv_pln=None
        if flat is None or measured <= 0
        else flat.cost(measured + sum(imported.values())),
    )


def _from_legacy(settlement: MonthSettlement, legacy: LegacyMonth) -> MonthlySavings:
    """Carry a pre-measurement month verbatim. W2 == W3 there — it was flat G11."""
    return MonthlySavings(
        month=settlement.month,
        self_consumption_kwh=legacy.self_consumption_kwh,
        self_consumption_pln=legacy.self_consumption_pln,
        self_consumption_g11_pln=legacy.self_consumption_pln,
        deposit_used_pln=settlement.used,
        import_kwh=legacy.import_kwh,
        paid_variable_pln=legacy.paid_pln,
        without_pv_pln=legacy.without_pv_pln,
    )


@dataclass(frozen=True)
class LegacyMonth:
    """A month from before hourly household data — figures carried, not recomputed."""

    self_consumption_kwh: float
    self_consumption_pln: float
    import_kwh: float
    without_pv_pln: float
    paid_pln: float


@dataclass(frozen=True)
class MonthlySavings:
    """One month of avoided cost, under both baselines."""

    month: BillingMonth
    self_consumption_kwh: float
    self_consumption_pln: float
    """W2 — self-consumption at the retail price of the zone it displaced."""
    self_consumption_g11_pln: float | None
    """W3 — the same volume at the flat G11 rate. Equals W2 in the flat-tariff era."""
    deposit_used_pln: float
    import_kwh: float
    paid_variable_pln: float
    """What the variable part of the bill actually came to, after the deposit."""
    without_pv_pln: float | None
    """Whole consumption on the flat G11 tariff, or None when not measurable."""

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
    def consumption_kwh(self) -> float:
        return self.self_consumption_kwh + self.import_kwh


@dataclass(frozen=True)
class SavingsReport:
    """Savings over the whole life of the installation."""

    months: tuple[MonthlySavings, ...]

    @property
    def total_pln(self) -> float:
        """Baseline A over the whole life."""
        return sum(month.total_pln for month in self.months)

    @property
    def self_consumption_pln(self) -> float:
        return sum(month.self_consumption_pln for month in self.months)

    @property
    def self_consumption_g11_pln(self) -> float:
        """W3 over the whole life — the same volumes priced on the flat tariff."""
        return sum(month.self_consumption_g11_pln or 0.0 for month in self.months)

    @property
    def deposit_pln(self) -> float:
        return sum(month.deposit_used_pln for month in self.months)

    @property
    def counterfactual_months(self) -> tuple[MonthlySavings, ...]:
        """Return the months where the "without PV" comparison can be made."""
        return tuple(m for m in self.months if m.without_pv_pln is not None)

    @property
    def without_pv_pln(self) -> float:
        """Lifetime counterfactual bill, over every month that has one."""
        return sum(m.without_pv_pln or 0.0 for m in self.counterfactual_months)

    @property
    def paid_pln(self) -> float:
        return sum(m.paid_variable_pln for m in self.counterfactual_months)

    @property
    def avoided_pln(self) -> float:
        return self.without_pv_pln - self.paid_pln

    def by_year(self) -> dict[int, float]:
        """Baseline A totals per calendar year."""
        totals: dict[int, float] = defaultdict(float)
        for month in self.months:
            totals[month.month.year] += month.total_pln
        return dict(sorted(totals.items()))
