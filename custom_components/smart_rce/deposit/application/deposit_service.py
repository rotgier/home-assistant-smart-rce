"""DepositService — rebuilds the settled ledger and everything derived from it.

Derives the whole picture from `SettlementHistory` on every recalculation, so
there is exactly one place where "what actually happened" lives and no chance of
the report drifting from it. `recalculate()` is what the daily refresh calls once
it has appended new days.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from ..domain.billing_month import BillingMonth
from ..domain.deposit_ledger import DepositLedger, MonthSettlement
from ..domain.projection import DepositProjection
from ..domain.reference_year import MonthRecord, ReferenceYear
from ..domain.savings import compute_savings
from ..domain.settlement_history import SettlementHistory
from ..domain.tariff import Tariff, Zone
from .report import DepositReport

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
        legacy_savings_pln: float = 0.0,
        consumption_factor: float = _CONSUMPTION_FACTOR,
        price_factor: float = _PRICE_FACTOR,
    ) -> None:
        self._tariff = tariff
        self._history = history
        self._legacy_savings_pln = legacy_savings_pln
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
            capacity=projection.capacity,
            oldest_tranche_age=ledger.oldest_tranche_age(last_settled),
            break_even_rce_net=self._tariff.latest.night_marginal_cost * _PLN_PER_MWH,
            history=settled,
            winter=projection.winter(ledger, after=last_settled),
            expiry=projection.expiry(ledger, after=last_settled),
            savings=compute_savings(
                settled,
                {record.month: record.import_kwh for record in self._history.months},
                self._self_consumption,
                self._tariff,
                self._legacy_savings_pln,
            ),
        )

    def _reference_partial(self) -> MonthRecord | None:
        """Scale the open month to a full one — the reference year needs whole months.

        Without it the profile falls back to the same calendar month a year ago,
        which silently understates anything that changed since (a new export
        strategy, most obviously).
        """
        partial = self._history.partial
        if partial is None or self._history.elapsed_days == 0:
            return None
        return partial.extrapolated(self._history.elapsed_days)

    def _running_balance(self, settled_balance: float) -> float:
        """Add what the open month has accrued to the settled balance.

        The settled figure is the one that reconciles with the invoice; this is
        the one that answers "how much do I have right now".
        """
        partial = self._history.partial
        if partial is None:
            return settled_balance
        rates = self._tariff.for_month(partial.month)
        return (
            settled_balance
            + partial.deposit_earned
            - rates.energy_cost(partial.import_kwh)
        )

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
