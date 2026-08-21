"""Savings — self-consumption valued per zone, plus the deposit actually used."""

import datetime

from custom_components.smart_rce.deposit.domain.billing_month import BillingMonth
from custom_components.smart_rce.deposit.domain.deposit_ledger import MonthSettlement
from custom_components.smart_rce.deposit.domain.savings import (
    LegacyEra,
    compute_savings,
)
from custom_components.smart_rce.deposit.domain.self_consumption import (
    HouseholdHour,
    self_consumption_by_zone,
)
from custom_components.smart_rce.deposit.domain.tariff import (
    FlatRates,
    Tariff,
    Zone,
    ZoneRates,
)
import pytest

_RATES = ZoneRates(
    energy={Zone.T1: 0.50, Zone.T2: 0.80, Zone.T3: 0.40},
    distribution={Zone.T1: 0.25, Zone.T2: 0.45, Zone.T3: 0.08},
)
_G11 = FlatRates(energy=0.60, distribution=0.30)
_TARIFF = Tariff({BillingMonth(2026, 1): _RATES}, {BillingMonth(2026, 1): _G11})
_TARIFF_NO_G11 = Tariff({BillingMonth(2026, 1): _RATES})
_NO_LEGACY = LegacyEra(self_consumption_pln=0.0, without_pv_pln=0.0, paid_pln=0.0)


def _settlement(month: BillingMonth, used: float) -> MonthSettlement:
    return MonthSettlement(
        month=month,
        earned=0.0,
        energy_cost=used,
        used=used,
        cash=0.0,
        refunded=0.0,
        forfeited=0.0,
        balance=0.0,
    )


def test_self_consumption_is_consumption_not_covered_by_import():
    hour = HouseholdHour(consumption_kwh=3.0, net_kwh=-1.0)  # imported 1 kWh

    assert hour.imported_kwh == pytest.approx(1.0)
    assert hour.self_consumed_kwh == pytest.approx(2.0)


def test_exporting_hours_are_fully_self_consumed():
    hour = HouseholdHour(consumption_kwh=2.0, net_kwh=5.0)

    assert hour.imported_kwh == 0.0
    assert hour.self_consumed_kwh == pytest.approx(2.0)


def test_self_consumption_lands_in_the_zone_of_its_hour():
    """Thursday in summer: 08:00 is T1, 20:00 is T2."""
    zones = self_consumption_by_zone(
        datetime.date(2026, 8, 20),
        {
            8: HouseholdHour(consumption_kwh=1.0, net_kwh=0.0),
            20: HouseholdHour(consumption_kwh=2.0, net_kwh=0.0),
        },
    )

    assert zones[Zone.T1] == pytest.approx(1.0)
    assert zones[Zone.T2] == pytest.approx(2.0)


def test_self_consumption_is_valued_at_full_retail_of_its_zone():
    """Energy AND distribution — that is what the kWh would have cost."""
    month = BillingMonth(2026, 1)

    report = compute_savings(
        [_settlement(month, used=0.0)],
        {},
        {month: {Zone.T1: 0.0, Zone.T2: 10.0, Zone.T3: 0.0}},
        _TARIFF,
        legacy=_NO_LEGACY,
    )

    assert report.months[0].self_consumption_pln == pytest.approx(
        10.0 * (0.80 + 0.45) * 1.23
    )


def test_a_kwh_in_the_evening_peak_is_worth_more_than_at_night():
    """The whole reason for pricing per zone rather than on a daily average."""
    month = BillingMonth(2026, 1)
    peak = compute_savings(
        [_settlement(month, 0.0)], {}, {month: {Zone.T2: 1.0}}, _TARIFF, _NO_LEGACY
    )
    night = compute_savings(
        [_settlement(month, 0.0)], {}, {month: {Zone.T3: 1.0}}, _TARIFF, _NO_LEGACY
    )

    assert peak.total_pln > 2 * night.total_pln


def test_total_is_self_consumption_plus_deposit_used():
    month = BillingMonth(2026, 1)

    report = compute_savings(
        [_settlement(month, used=100.0)],
        {},
        {month: {Zone.T3: 10.0}},
        _TARIFF,
        _NO_LEGACY,
    )

    assert report.months[0].deposit_used_pln == pytest.approx(100.0)
    assert report.total_pln == pytest.approx(
        report.self_consumption_pln + report.deposit_pln
    )


def test_legacy_lump_counts_as_self_consumption_not_deposit():
    month = BillingMonth(2026, 1)

    report = compute_savings(
        [_settlement(month, used=50.0)],
        {},
        {},
        _TARIFF,
        legacy=LegacyEra(6018.46, 0.0, 0.0),
    )

    assert report.self_consumption_pln == pytest.approx(6018.46)
    assert report.deposit_pln == pytest.approx(50.0)
    assert report.total_pln == pytest.approx(6068.46)


def test_months_without_measured_volumes_still_count_their_deposit():
    """The pre-recorder era has no zonal data but did use deposit."""
    month = BillingMonth(2026, 1)

    report = compute_savings(
        [_settlement(month, used=42.0)], {}, {}, _TARIFF, _NO_LEGACY
    )

    assert report.months[0].self_consumption_pln == 0.0
    assert report.months[0].total_pln == pytest.approx(42.0)


def test_by_year_groups_measured_months():
    report = compute_savings(
        [
            _settlement(BillingMonth(2025, 12), used=10.0),
            _settlement(BillingMonth(2026, 1), used=20.0),
            _settlement(BillingMonth(2026, 2), used=30.0),
        ],
        {},
        {},
        _TARIFF,
        _NO_LEGACY,
    )

    assert report.by_year() == {2025: pytest.approx(10.0), 2026: pytest.approx(50.0)}


class TestCounterfactual:
    """Baseline B — the bill that would have arrived without the installation."""

    MONTH = BillingMonth(2026, 1)

    def _report(self, tariff=_TARIFF, used=0.0):
        return compute_savings(
            [_settlement(self.MONTH, used=used)],
            {self.MONTH: {Zone.T1: 0.0, Zone.T2: 0.0, Zone.T3: 100.0}},
            {self.MONTH: {Zone.T1: 0.0, Zone.T2: 0.0, Zone.T3: 200.0}},
            tariff,
            legacy=_NO_LEGACY,
        )

    def test_prices_the_whole_household_consumption_on_the_flat_tariff(self):
        """300 kWh consumed (200 self + 100 imported) at the G11 rate."""
        month = self._report().months[0]

        assert month.consumption_kwh == pytest.approx(300.0)
        assert month.without_pv_pln == pytest.approx(300.0 * (0.60 + 0.30) * 1.23)

    def test_what_was_paid_is_the_import_bill_after_the_deposit(self):
        month = self._report(used=30.0).months[0]

        assert month.paid_variable_pln == pytest.approx(
            100.0 * (0.40 + 0.08) * 1.23 - 30.0
        )

    def test_avoided_is_the_difference_between_the_two_bills(self):
        month = self._report(used=30.0).months[0]

        assert month.avoided_pln == pytest.approx(
            month.without_pv_pln - month.paid_variable_pln
        )

    def test_counterfactual_beats_the_same_tariff_baseline(self):
        """G11 is dearer than the night zone, so baseline B is the larger figure."""
        month = self._report(used=30.0).months[0]

        assert month.avoided_pln > month.total_pln

    def test_months_without_measured_self_consumption_have_no_counterfactual(self):
        """Household total is unknown there — better absent than invented."""
        report = compute_savings(
            [_settlement(self.MONTH, used=10.0)], {}, {}, _TARIFF, 0.0
        )

        assert report.months[0].without_pv_pln is None
        assert report.months[0].avoided_pln is None
        assert report.counterfactual_months == ()

    def test_no_flat_tariff_means_no_counterfactual(self):
        assert self._report(tariff=_TARIFF_NO_G11).months[0].without_pv_pln is None


def test_legacy_era_is_part_of_the_lifetime_counterfactual():
    """Otherwise the dashboard silently understates the installation's whole life.

    That era ran on the flat tariff anyway, so its counterfactual is as solid as a
    measured month — it just cannot be recomputed from data HA never had.
    """
    month = BillingMonth(2026, 1)
    legacy = LegacyEra(
        self_consumption_pln=6018.46, without_pv_pln=8241.20, paid_pln=1867.36
    )

    report = compute_savings(
        [_settlement(month, used=0.0)],
        {month: {Zone.T3: 100.0}},
        {month: {Zone.T3: 200.0}},
        _TARIFF,
        legacy,
    )

    measured = report.months[0]
    assert report.without_pv_pln == pytest.approx(measured.without_pv_pln + 8241.20)
    assert report.paid_pln == pytest.approx(measured.paid_variable_pln + 1867.36)
    assert report.avoided_pln == pytest.approx(
        measured.avoided_pln + legacy.avoided_pln
    )


def test_the_three_lifetime_figures_stay_consistent():
    """The card shows all three side by side; they must subtract to each other."""
    month = BillingMonth(2026, 1)

    report = compute_savings(
        [_settlement(month, used=15.0)],
        {month: {Zone.T3: 100.0}},
        {month: {Zone.T3: 200.0}},
        _TARIFF,
        LegacyEra(6018.46, 8241.20, 1867.36),
    )

    assert report.avoided_pln == pytest.approx(report.without_pv_pln - report.paid_pln)
