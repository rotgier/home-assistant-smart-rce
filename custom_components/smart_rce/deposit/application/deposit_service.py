"""DepositService — rebuilds the settled ledger and everything derived from it.

Derives the whole picture from `SettlementHistory` on every recalculation, so
there is exactly one place where "what actually happened" lives and no chance of
the report drifting from it. `recalculate()` is what the daily refresh calls once
it has appended new days.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Final

from ...tariff import Zone
from ..domain.billing_month import BillingMonth
from ..domain.deposit_ledger import DepositLedger, MonthSettlement
from ..domain.market_price import MonthlyMarketPrices
from ..domain.projection import DepositProjection
from ..domain.reference_year import MonthRecord, ReferenceYear
from ..domain.savings import LegacyMonth, compute_savings
from ..domain.settlement_history import DayRecord, SettlementHistory, sum_days
from ..domain.tariff import Tariff
from .report import DepositReport, MonthlyVolumes, OpenMonth

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_PLN_PER_MWH: Final = 1000.0


class DepositService:
    """Owns the current `DepositReport`."""

    # Consumption is assumed slightly above the realised year — the projection
    # should not read as optimistic. Prices are held flat: guessing a tariff is
    # worse than showing what today's tariff implies, and a rise works in our
    # favour anyway (it enlarges the capacity that absorbs the deposit).
    _CONSUMPTION_FACTOR: Final = 1.04
    _PRICE_FACTOR: Final = 1.00

    def __init__(
        self,
        tariff: Tariff,
        history: SettlementHistory,
        *,
        legacy: Mapping[BillingMonth, LegacyMonth] | None = None,
        monthly_prices: MonthlyMarketPrices | None = None,
        seed_production: Mapping[BillingMonth, float] | None = None,
        consumption_factor: float = _CONSUMPTION_FACTOR,
        price_factor: float = _PRICE_FACTOR,
    ) -> None:
        self._tariff = tariff
        self._history = history
        self._legacy = dict(legacy or {})
        self._shipped_prices = monthly_prices or MonthlyMarketPrices()
        self._monthly_prices = self._shipped_prices
        self._seed_production = dict(seed_production or {})
        self._measured_production: dict[BillingMonth, float] = {}
        self._last_year_days: dict[datetime.date, DayRecord] = {}
        self._self_consumption: dict[BillingMonth, Mapping[Zone, float]] = {}
        self._consumption_factor = consumption_factor
        self._price_factor = price_factor
        self._report = self._build()
        self._listeners: list[Callable[[], None]] = []

    @property
    def report(self) -> DepositReport:
        return self._report

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to recalculations. Returns an unsubscribe callable."""
        self._listeners.append(listener)

        def _unsubscribe() -> None:
            self._listeners.remove(listener)

        return _unsubscribe

    def update_market_prices(self, prices: Mapping[BillingMonth, float]) -> None:
        """Layer prices published since the table was shipped, and rebuild."""
        self._monthly_prices = self._shipped_prices.merged_with(prices)
        self.recalculate()

    def update_last_year_days(self, days: Mapping[datetime.date, DayRecord]) -> None:
        """Supply last year's days, re-dated to this year, and rebuild.

        They fill the part of the current month that has not happened yet, so the
        reference year stops inventing it. Keyed by the date they stand in for.
        """
        self._last_year_days = dict(days)
        self.recalculate()

    def update_production(self, by_month: Mapping[BillingMonth, float]) -> None:
        """Replace the measured PV production and rebuild.

        Measured months win over the seed, which only covers the era before the
        recorder existed.
        """
        self._measured_production = dict(by_month)
        self.recalculate()

    def update_self_consumption(
        self, by_month: Mapping[BillingMonth, Mapping[Zone, float]]
    ) -> None:
        """Replace the measured self-consumption volumes and rebuild."""
        self._self_consumption = dict(by_month)
        self.recalculate()

    def recalculate(self) -> None:
        """Rebuild the snapshot and wake the entities reading it."""
        self._report = self._build()
        for listener in list(self._listeners):
            listener()

    def _build(self) -> DepositReport:
        ledger, settled = self._replay()
        projection = DepositProjection(
            ReferenceYear.from_history(
                self._history.months, partial=self._reference_partial()
            ),
            self._tariff.latest,
            consumption_factor=self._consumption_factor,
            price_factor=self._price_factor,
        )
        last_settled = settled[-1].month
        return DepositReport(
            last_settled=last_settled,
            balance=ledger.balance,
            balance_running=self._running_balance(ledger.balance),
            last_data_day=self._history.last_data_day,
            elapsed_days=self._history.elapsed_days,
            unsettled_days=len(self._history.unsettled_days),
            capacity=projection.capacity,
            oldest_tranche_age=ledger.oldest_tranche_age(last_settled),
            break_even_rce_net=self._tariff.latest.night_marginal_cost * _PLN_PER_MWH,
            history=settled,
            open_months=self._open_months(ledger.balance),
            volumes=self._volumes(),
            winter=projection.winter(ledger, after=last_settled),
            expiry=projection.expiry(ledger, after=last_settled),
            savings=compute_savings(
                settled,
                {record.month: record.import_kwh for record in self._history.months},
                self._self_consumption,
                self._tariff,
                self._legacy,
            ),
        )

    def _open_months(self, settled_balance: float) -> tuple[OpenMonth, ...]:
        """Describe every month measured but not settled, balance running through.

        More than one whenever a month has ended but not yet finalised — the week
        in which its days can still come back from the meter.
        """
        months: list[OpenMonth] = []
        balance = settled_balance
        for record in self._history.open_months:
            energy_cost = self._tariff.for_month(record.month).energy_cost(
                record.import_kwh
            )
            balance += record.deposit_earned - energy_cost
            months.append(
                OpenMonth(
                    month=record.month,
                    elapsed_days=self._history.measured_days(record.month),
                    exported_kwh=record.exported_kwh,
                    earned=record.deposit_earned,
                    energy_cost=energy_cost,
                    balance=balance,
                )
            )
        return tuple(months)

    def _volumes(self) -> dict[BillingMonth, MonthlyVolumes]:
        """Join measured energy with the RCEm counterfactual, month by month."""
        production = {**self._seed_production, **self._measured_production}
        return {
            record.month: MonthlyVolumes(
                exported_kwh=record.exported_kwh,
                import_kwh=record.total_import_kwh,
                production_kwh=production.get(record.month),
                deposit_at_monthly_price=self._monthly_prices.deposit_for(
                    record.month, record.exported_kwh
                ),
            )
            for record in self._history.months
        }

    def _reference_partial(self) -> MonthRecord | None:
        """Build the current month whole: days that happened, plus days that have not.

        The reference year needs full months, and the current one is half-written.
        Days already measured come from this year; the rest are the very same dates
        a year ago, taken as they were — not a month total spread evenly over them.

        Spreading was the first idea and the measurements killed it: within one
        month the shape is set by weather, not by the calendar. October 2024 put
        63% of its export in the second half, November 2025 put 76% in the first.
        No scaling reproduces that; the actual days do.

        Scaling the whole month up from the days so far — what this did until
        2026-10-03 — was worse still. Two sunny days of October became 676 kWh,
        three times the real October before it, and the winter trough jumped with it.
        """
        partial = self._history.partial
        if partial is None:
            return None
        month = partial.month
        measured = {record.day.day: record for record in self._history.days_in(month)}
        composed = [
            measured.get(number)
            or self._last_year_days.get(datetime.date(month.year, month.month, number))
            for number in range(1, month.days + 1)
        ]
        present = [record for record in composed if record is not None]
        return sum_days(month, present) if present else None

    def _running_balance(self, settled_balance: float) -> float:
        """Add everything measured but not yet settled to the settled balance.

        The settled figure is the one that reconciles with the invoice; this is
        the one that answers "how much do I have right now".

        Every open month, not just the newest: a month closes a week after it
        ends, so in the first days of a new one two are open at once. Counting
        only the newest dropped all of September here on 2026-10-02.
        """
        balance = settled_balance
        for record in self._history.open_months:
            rates = self._tariff.for_month(record.month)
            balance += record.deposit_earned - rates.energy_cost(record.import_kwh)
        return balance

    def _replay(self) -> tuple[DepositLedger, tuple[MonthSettlement, ...]]:
        """Settle every closed month in order — reproduces the invoiced history."""
        ledger = DepositLedger()
        settled = [
            ledger.settle(
                record.month,
                record.deposit_earned,
                self._tariff.for_month(record.month).energy_cost(record.import_kwh),
            )
            for record in self._history.months
        ]
        return ledger, tuple(settled)
