"""DepositLedger — FIFO consumption, the M+1 availability lag, and expiry."""

from custom_components.smart_rce.deposit.domain.billing_month import BillingMonth
from custom_components.smart_rce.deposit.domain.deposit_ledger import DepositLedger
import pytest


def _month(text: str) -> BillingMonth:
    return BillingMonth.parse(text)


def test_deposit_earned_this_month_cannot_pay_this_month():
    """The M+1 lag: March is paid out of February's balance, not March's own."""
    ledger = DepositLedger()

    february = ledger.settle(_month("2027-02"), earned=100.0, energy_cost=0.0)
    march = ledger.settle(_month("2027-03"), earned=180.0, energy_cost=150.0)

    assert february.used == 0.0
    assert march.used == pytest.approx(100.0)  # only February's tranche was available
    assert march.cash == pytest.approx(50.0)


def test_consumes_oldest_tranche_first():
    ledger = DepositLedger([(_month("2026-05"), 50.0), (_month("2026-06"), 80.0)])

    settlement = ledger.settle(_month("2026-07"), earned=0.0, energy_cost=70.0)

    assert settlement.used == pytest.approx(70.0)
    # 50 from May (drained) + 20 from June -> 60 left, all of it June's
    assert ledger.balance == pytest.approx(60.0)
    assert ledger.oldest_tranche_age(_month("2026-07")) == 1


def test_cash_is_charged_only_beyond_the_balance():
    ledger = DepositLedger([(_month("2026-05"), 30.0)])

    settlement = ledger.settle(_month("2026-06"), earned=0.0, energy_cost=100.0)

    assert settlement.used == pytest.approx(30.0)
    assert settlement.cash == pytest.approx(70.0)
    assert ledger.balance == pytest.approx(0.0)


def test_tranche_expires_after_twelve_months_with_hourly_refund_share():
    ledger = DepositLedger([(_month("2026-08"), 200.0)])

    settlement = ledger.settle(_month("2027-08"), earned=0.0, energy_cost=0.0)

    assert settlement.refunded == pytest.approx(60.0)  # 30% — hourly RCE regime
    assert settlement.forfeited == pytest.approx(140.0)
    assert ledger.balance == pytest.approx(0.0)


def test_tranche_earned_before_february_2025_refunds_twenty_percent():
    ledger = DepositLedger([(_month("2025-01"), 200.0)])

    settlement = ledger.settle(_month("2026-01"), earned=0.0, energy_cost=0.0)

    assert settlement.refunded == pytest.approx(40.0)
    assert settlement.forfeited == pytest.approx(160.0)


def test_tranche_one_month_short_of_expiry_survives():
    ledger = DepositLedger([(_month("2026-08"), 200.0)])

    settlement = ledger.settle(_month("2027-07"), earned=0.0, energy_cost=0.0)

    assert settlement.forfeited == 0.0
    assert ledger.balance == pytest.approx(200.0)
    assert ledger.oldest_tranche_age(_month("2027-07")) == 11


def test_expiry_runs_before_consumption():
    """A tranche that ages out cannot pay the same month's bill."""
    ledger = DepositLedger([(_month("2026-08"), 200.0)])

    settlement = ledger.settle(_month("2027-08"), earned=0.0, energy_cost=200.0)

    assert settlement.used == 0.0
    assert settlement.cash == pytest.approx(200.0)


def test_round_trips_through_storage():
    ledger = DepositLedger([(_month("2026-05"), 12.5), (_month("2026-06"), 7.25)])

    restored = DepositLedger.from_dict(ledger.to_dict())

    assert restored.balance == pytest.approx(ledger.balance)
    assert restored.to_dict() == ledger.to_dict()


def test_copy_is_detached():
    ledger = DepositLedger([(_month("2026-05"), 100.0)])

    ledger.copy().settle(_month("2026-06"), earned=0.0, energy_cost=100.0)

    assert ledger.balance == pytest.approx(100.0)


def test_empty_ledger_has_no_oldest_tranche():
    assert DepositLedger().oldest_tranche_age(_month("2026-07")) is None
