"""DepositProjection — winter coverage and the forfeiting horizon.

Includes an acceptance test that replays the shipped seed: the domain must
reproduce the standalone calculator (`fotowoltaika/depozyt`) to the grosz,
because that history is reconciled against actual TAURON invoices.
"""

from custom_components.smart_rce.deposit.application.deposit_service import (
    DepositService,
)
from custom_components.smart_rce.deposit.domain.billing_month import BillingMonth
from custom_components.smart_rce.deposit.domain.capacity import ConsumptionCapacity
from custom_components.smart_rce.deposit.domain.deposit_ledger import DepositLedger
from custom_components.smart_rce.deposit.domain.projection import DepositProjection
from custom_components.smart_rce.deposit.domain.reference_year import (
    MonthRecord,
    ReferenceYear,
)
from custom_components.smart_rce.deposit.domain.tariff import Zone, ZoneRates
from custom_components.smart_rce.deposit.infrastructure.resources import (
    load_seed_history,
    load_tariff,
)
import pytest

_FLAT_RATES = ZoneRates(
    energy=dict.fromkeys(Zone, 0.5),
    distribution=dict.fromkeys(Zone, 0.1),
)


def _flat_year(*, import_kwh: float, earned: float) -> ReferenceYear:
    """Twelve identical months — isolates the mechanics from seasonal shape."""
    return ReferenceYear.from_history(
        [
            MonthRecord(
                month=BillingMonth(2026, number),
                exported_kwh=0.0,
                deposit_earned=earned,
                import_kwh={Zone.T1: 0.0, Zone.T2: 0.0, Zone.T3: import_kwh},
            )
            for number in range(1, 13)
        ]
    )


def test_capacity_is_the_twelve_month_energy_bill():
    year = _flat_year(import_kwh=100.0, earned=0.0)

    capacity = ConsumptionCapacity.from_reference_year(year, _FLAT_RATES)

    assert capacity.value == pytest.approx(12 * 100.0 * 0.5 * 1.23)


def test_capacity_scales_with_the_consumption_factor():
    year = _flat_year(import_kwh=100.0, earned=0.0)

    capacity = ConsumptionCapacity.from_reference_year(
        year, _FLAT_RATES, consumption_factor=1.10
    )

    assert capacity.value == pytest.approx(12 * 100.0 * 0.5 * 1.23 * 1.10)


def test_break_even_threshold_ignores_vat():
    """Deposit and cost are both gross, so the 1.23 factor cancels out."""
    assert _FLAT_RATES.night_marginal_cost == pytest.approx(0.6)


def test_winter_reports_uncovered_when_the_balance_runs_out():
    year = _flat_year(import_kwh=100.0, earned=0.0)
    projection = DepositProjection(year, _FLAT_RATES)

    outlook = projection.winter(DepositLedger(), after=BillingMonth(2026, 9), months=3)

    assert not outlook.covered
    assert outlook.cash_total > 0


def test_winter_does_not_mutate_the_real_ledger():
    year = _flat_year(import_kwh=100.0, earned=0.0)
    ledger = DepositLedger([(BillingMonth(2026, 9), 500.0)])

    DepositProjection(year, _FLAT_RATES).winter(ledger, after=BillingMonth(2026, 9))

    assert ledger.balance == pytest.approx(500.0)


def test_expiry_detects_forfeiting_once_earning_outruns_capacity():
    """Earn far more than can ever be spent -> tranches must start ageing out."""
    year = _flat_year(import_kwh=10.0, earned=500.0)
    projection = DepositProjection(year, _FLAT_RATES)

    outlook = projection.expiry(DepositLedger(), after=BillingMonth(2026, 8), years=3)

    assert outlook.first_forfeit is not None
    assert outlook.peaks[-1].utilization > 100


def test_expiry_finds_nothing_while_the_balance_stays_below_capacity():
    year = _flat_year(import_kwh=100.0, earned=50.0)
    projection = DepositProjection(year, _FLAT_RATES)

    outlook = projection.expiry(DepositLedger(), after=BillingMonth(2026, 8), years=3)

    assert outlook.first_forfeit is None


class TestSeedAcceptance:
    """The shipped seed must reproduce the invoice-reconciled calculator."""

    LAST_SETTLED = BillingMonth(2026, 7)
    EXPECTED_BALANCE = 1092.19
    EXPECTED_CAPACITY = 2558.35
    EXPECTED_TROUGH = 391.08
    CONSUMPTION_FACTOR = 1.04

    @pytest.fixture
    def replayed(self):
        seed, tariff = load_seed_history(), load_tariff()
        ledger = DepositLedger()
        for record in seed.months:
            ledger.settle(
                record.month,
                record.deposit_earned,
                tariff.for_month(record.month).energy_cost(record.import_kwh),
            )
        year = ReferenceYear.from_history(seed.months, partial=seed.partial)
        projection = DepositProjection(
            year, tariff.latest, consumption_factor=self.CONSUMPTION_FACTOR
        )
        return ledger, projection

    def test_replaying_the_seed_reproduces_the_calculator_balance(self, replayed):
        ledger, _ = replayed
        assert ledger.balance == pytest.approx(self.EXPECTED_BALANCE, abs=0.01)

    def test_capacity_matches_the_calculator(self, replayed):
        _, projection = replayed
        assert projection.capacity.value == pytest.approx(
            self.EXPECTED_CAPACITY, abs=0.01
        )

    def test_winter_is_covered_with_the_expected_trough(self, replayed):
        ledger, projection = replayed
        outlook = projection.winter(ledger, after=self.LAST_SETTLED)
        assert outlook.covered
        assert outlook.trough.month == BillingMonth(2027, 2)
        assert outlook.trough.settlement.balance == pytest.approx(
            self.EXPECTED_TROUGH, abs=0.01
        )

    def test_forfeiting_starts_in_2031(self, replayed):
        ledger, projection = replayed
        outlook = projection.expiry(ledger, after=self.LAST_SETTLED)
        assert outlook.first_forfeit == BillingMonth(2031, 8)

    def test_break_even_is_reported_gross_and_net(self, replayed):
        """Gross is the headline — the rest of the system quotes RCE x 1.23.

        Gross must equal the gross retail price of the kWh re-bought at night,
        which is what makes the rule readable: export whenever you are paid more
        per kWh than you will pay for it at night.
        """
        _, projection = replayed
        rates = load_tariff().latest
        report = self._report(replayed)
        assert report.break_even_rce_net == pytest.approx(
            rates.night_marginal_cost * 1000
        )
        assert report.break_even_rce_gross == pytest.approx(
            report.break_even_rce_net * 1.23
        )
        assert report.to_dict()["break_even_rce_gross_pln_mwh"] == 626

    def _report(self, replayed):

        return DepositService(load_tariff(), load_seed_history()).report

    def test_current_utilization_leaves_headroom(self, replayed):
        ledger, projection = replayed
        assert projection.capacity.utilization(ledger.balance) < 100
