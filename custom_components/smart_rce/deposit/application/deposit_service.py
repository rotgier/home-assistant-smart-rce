"""DepositService — rebuilds the settled ledger and everything derived from it.

Phase 1 derives the whole picture from the shipped seed on every recalculation:
no persistence, no drift. When the eLicznik reader lands (phase 2) the seed
becomes the starting point and freshly closed months are appended to it — the
rest of this service does not change.
"""

from __future__ import annotations

from typing import Final

from ..domain.deposit_ledger import DepositLedger, MonthSettlement
from ..domain.projection import DepositProjection
from ..domain.reference_year import ReferenceYear
from ..domain.tariff import Tariff
from ..infrastructure.resources import SeedHistory
from .report import DepositReport

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
        seed: SeedHistory,
        *,
        consumption_factor: float = _CONSUMPTION_FACTOR,
        price_factor: float = _PRICE_FACTOR,
    ) -> None:
        self._tariff = tariff
        self._seed = seed
        self._consumption_factor = consumption_factor
        self._price_factor = price_factor
        self._report = self._build()

    @property
    def report(self) -> DepositReport:
        return self._report

    def recalculate(self) -> None:
        """Rebuild the snapshot — called by the daily refresh and the button."""
        self._report = self._build()

    def _build(self) -> DepositReport:
        ledger, history = self._replay()
        projection = DepositProjection(
            ReferenceYear.from_history(self._seed.months, partial=self._seed.partial),
            self._tariff.latest,
            consumption_factor=self._consumption_factor,
            price_factor=self._price_factor,
        )
        last_settled = history[-1].month
        return DepositReport(
            last_settled=last_settled,
            balance=ledger.balance,
            capacity=projection.capacity,
            oldest_tranche_age=ledger.oldest_tranche_age(last_settled),
            break_even_rce_net=self._tariff.latest.night_marginal_cost * _PLN_PER_MWH,
            history=history,
            winter=projection.winter(ledger, after=last_settled),
            expiry=projection.expiry(ledger, after=last_settled),
        )

    def _replay(self) -> tuple[DepositLedger, tuple[MonthSettlement, ...]]:
        """Settle every closed month in order — reproduces the invoiced history."""
        ledger = DepositLedger()
        history = [
            ledger.settle(
                record.month,
                record.deposit_earned,
                self._tariff.for_month(record.month).energy_cost(record.import_kwh),
            )
            for record in self._seed.months
        ]
        return ledger, tuple(history)
