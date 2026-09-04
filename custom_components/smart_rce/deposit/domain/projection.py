"""DepositProjection — repeats the reference year forward over a ledger copy.

Answers the two decision questions, both of which the raw yearly balance hides:

- `winter()` — does the balance survive to the spring turning point? The test is
  not the annual balance but the month-by-month one, because deposit earned in M
  is only spendable from M+1: March is paid out of February's balance.
- `expiry()` — in which year does a tranche first reach twelve months and start
  forfeiting? That is the signal to stop maximising exports.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Final

from .billing_month import BillingMonth
from .capacity import ConsumptionCapacity
from .deposit_ledger import DepositLedger, MonthSettlement
from .reference_year import ReferenceYear
from .tariff import ZoneRates


class DepositProjection:
    """Forward simulation. Never mutates the ledger it is given."""

    _WINTER_MONTHS: Final = 9
    _EXPIRY_YEARS: Final = 6
    _PEAK_MONTH: Final = 8  # August — the yearly balance maximum

    def __init__(
        self,
        reference_year: ReferenceYear,
        rates: ZoneRates,
        *,
        consumption_factor: float = 1.0,
        price_factor: float = 1.0,
    ) -> None:
        self._reference_year = reference_year
        self._rates = rates
        self._consumption_factor = consumption_factor
        self._price_factor = price_factor

    @property
    def capacity(self) -> ConsumptionCapacity:
        return ConsumptionCapacity.from_reference_year(
            self._reference_year,
            self._rates,
            consumption_factor=self._consumption_factor,
        )

    def winter(
        self, ledger: DepositLedger, *, after: BillingMonth, months: int | None = None
    ) -> WinterOutlook:
        """Month-by-month from `after`+1 through the spring turning point."""
        projected = self._run(ledger, after=after, months=months or self._WINTER_MONTHS)
        trough = min(projected, key=lambda month: month.settlement.balance)
        return WinterOutlook(
            months=tuple(projected),
            trough=trough,
            cash_total=sum(month.settlement.cash for month in projected),
        )

    def expiry(
        self, ledger: DepositLedger, *, after: BillingMonth, years: int | None = None
    ) -> ExpiryOutlook:
        """Repeat the reference year until a tranche forfeits (or the horizon ends)."""
        capacity = self.capacity
        projected = self._run(
            ledger, after=after, months=(years or self._EXPIRY_YEARS) * 12
        )
        # Forfeiting is per tranche, twelve months after it was earned, so it
        # lands wherever the summer's deposit turns a year old — September, as a
        # rule, right after the peak. Reporting only the peak month's own figure
        # showed a column of zeros next to a headline saying the opposite.
        forfeited: dict[int, float] = defaultdict(float)
        refunded: dict[int, float] = defaultdict(float)
        for month in projected:
            forfeited[month.month.year] += month.settlement.forfeited
            refunded[month.month.year] += month.settlement.refunded
        peaks = tuple(
            YearPeak(
                year=month.month.year,
                balance=month.settlement.balance,
                utilization=capacity.utilization(month.settlement.balance),
                forfeited=forfeited[month.month.year],
                refunded=refunded[month.month.year],
            )
            for month in projected
            if month.month.month == self._PEAK_MONTH
        )
        first = next((m.month for m in projected if m.settlement.forfeited > 0), None)
        return ExpiryOutlook(capacity=capacity, peaks=peaks, first_forfeit=first)

    def _run(
        self, ledger: DepositLedger, *, after: BillingMonth, months: int
    ) -> list[ProjectedMonth]:
        working = ledger.copy()
        projected: list[ProjectedMonth] = []
        month = after
        for _ in range(months):
            month = month.next()
            projected.append(self._project(working, month))
        return projected

    def _project(self, ledger: DepositLedger, month: BillingMonth) -> ProjectedMonth:
        record = self._reference_year.for_month(month)
        import_kwh = {
            zone: kwh * self._consumption_factor
            for zone, kwh in record.import_kwh.items()
        }
        available = ledger.balance
        settlement = ledger.settle(
            month,
            earned=record.deposit_earned * self._price_factor,
            energy_cost=self._rates.energy_cost(import_kwh),
        )
        return ProjectedMonth(
            month=month,
            exported_kwh=record.exported_kwh,
            import_kwh=sum(import_kwh.values()),
            distribution_cost=self._rates.distribution_cost(import_kwh),
            available=available,
            settlement=settlement,
        )


@dataclass(frozen=True)
class WinterOutlook:
    """Whether the balance carries the household to the spring turning point."""

    months: tuple[ProjectedMonth, ...]
    trough: ProjectedMonth
    cash_total: float

    @property
    def covered(self) -> bool:
        """True when no month needs cash for active energy."""
        return self.cash_total < 0.01


@dataclass(frozen=True)
class ExpiryOutlook:
    """When the account outgrows what it can spend."""

    capacity: ConsumptionCapacity
    peaks: tuple[YearPeak, ...]
    first_forfeit: BillingMonth | None


@dataclass(frozen=True)
class ProjectedMonth:
    """One simulated month."""

    month: BillingMonth
    exported_kwh: float
    import_kwh: float
    distribution_cost: float
    available: float
    settlement: MonthSettlement


@dataclass(frozen=True)
class YearPeak:
    """One year: its balance at the maximum, and what it lost over the whole year."""

    year: int
    balance: float
    """End of August — the yearly maximum, which is what decides forfeiting."""
    utilization: float
    forfeited: float
    """Whole year, not just August: a tranche expires wherever it turns twelve
    months old, which is normally September."""
    refunded: float
