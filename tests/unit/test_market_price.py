"""RCEm — pricing the same exported volumes under the monthly regime."""

import datetime

from custom_components.smart_rce.deposit.application.deposit_service import (
    DepositService,
)
from custom_components.smart_rce.deposit.application.production_service import (
    FIRST_MEASURED_MONTH,
    ProductionService,
)
from custom_components.smart_rce.deposit.domain.billing_month import BillingMonth
from custom_components.smart_rce.deposit.domain.market_price import MonthlyMarketPrices
from custom_components.smart_rce.deposit.domain.reference_year import MonthRecord
from custom_components.smart_rce.deposit.domain.settlement_history import (
    SettlementHistory,
)
from custom_components.smart_rce.deposit.domain.tariff import (
    FlatRates,
    Tariff,
    Zone,
    ZoneRates,
)
import pytest

_RATES = ZoneRates(
    energy=dict.fromkeys(Zone, 0.5),
    distribution=dict.fromkeys(Zone, 0.1),
)
_TARIFF = Tariff(
    {BillingMonth(2024, 1): _RATES},
    {BillingMonth(2024, 1): FlatRates(energy=0.6, distribution=0.3)},
)


def _history(*months: BillingMonth) -> SettlementHistory:
    """Twelve months ending with the newest one asked for, so projections work."""
    newest = max(months)
    wanted = set(months)
    records = []
    cursor = newest
    for _ in range(12):
        records.append(
            MonthRecord(
                month=cursor,
                exported_kwh=100.0 if cursor in wanted else 10.0,
                deposit_earned=50.0,
                import_kwh=dict.fromkeys(Zone, 10.0),
            )
        )
        cursor = cursor.previous()
    return SettlementHistory(
        reversed(records), last_data_day=datetime.date(2026, 1, 31)
    )


class TestMonthlyMarketPrices:
    def test_prices_the_export_at_the_month_s_own_price(self):
        month = BillingMonth(2024, 8)
        prices = MonthlyMarketPrices({month: 300.0})

        assert prices.deposit_for(month, 1000.0) == pytest.approx(300.0)

    def test_applies_the_coefficient_of_the_regime_in_force(self):
        """The counterfactual is monthly *pricing*, not the old coefficient."""
        month = BillingMonth(2025, 3)
        prices = MonthlyMarketPrices({month: 300.0})

        assert prices.deposit_for(month, 1000.0) == pytest.approx(300.0 * 1.23)

    def test_the_pre_hourly_era_has_nothing_to_compare(self):
        """Then the monthly price *was* the settlement — the bars would be equal."""
        month = BillingMonth(2024, 6)

        assert MonthlyMarketPrices({month: 300.0}).deposit_for(month, 1000.0) is None

    def test_a_month_without_a_published_price_drops_out(self):
        """PSE publishes around the 11th — the newest month never has one yet."""
        assert MonthlyMarketPrices().deposit_for(BillingMonth(2026, 7), 1000.0) is None


class TestReportedVolumes:
    """What the ledger settled, joined with the energy it was derived from."""

    MONTH = BillingMonth(2026, 1)

    def _row(self, **kwargs) -> dict:
        service = DepositService(_TARIFF, _history(self.MONTH), **kwargs)
        rows = service.report.to_dict()["history"]
        return next(row for row in rows if row["month"] == str(self.MONTH))

    def test_a_history_row_carries_the_energy_behind_it(self):
        row = self._row()

        assert row["exported_kwh"] == pytest.approx(100.0)
        assert row["import_kwh"] == pytest.approx(30.0)

    def test_the_monthly_price_counterfactual_rides_along(self):
        row = self._row(monthly_prices=MonthlyMarketPrices({self.MONTH: 200.0}))

        assert row["earned_at_monthly_price"] == pytest.approx(100.0 * 0.2 * 1.23)

    def test_production_comes_from_the_seed_where_nothing_measured_it(self):
        row = self._row(seed_production={self.MONTH: 777.0})

        assert row["production_kwh"] == pytest.approx(777.0)

    def test_a_measured_month_overrides_the_seed(self):
        """The seed only covers the era before the recorder existed."""
        service = DepositService(
            _TARIFF, _history(self.MONTH), seed_production={self.MONTH: 777.0}
        )

        service.update_production({self.MONTH: 900.0})

        rows = service.report.to_dict()["history"]
        row = next(r for r in rows if r["month"] == str(self.MONTH))
        assert row["production_kwh"] == pytest.approx(900.0)

    def test_an_unknown_month_reports_no_production_rather_than_zero(self):
        """Zero would read as "the sun did not shine", which is a different claim."""
        assert self._row()["production_kwh"] is None


class _FakeReader:
    def __init__(self, months=None):
        self.months = months or {}
        self.asked = None

    async def async_months_between(self, start, end):
        self.asked = (start, end)
        return self.months


class TestProductionService:
    async def test_hands_measured_months_to_the_report(self):
        month = BillingMonth(2026, 1)
        service = DepositService(_TARIFF, _history(month))
        reader = _FakeReader({month: 640.0})

        await ProductionService(reader, service).async_refresh(
            datetime.date(2026, 2, 1)
        )

        rows = service.report.to_dict()["history"]
        row = next(r for r in rows if r["month"] == str(month))
        assert row["production_kwh"] == pytest.approx(640.0)

    async def test_reads_from_the_first_month_the_recorder_has(self):
        """Earlier months exist only in the inverter's history, so they are seeded."""
        reader = _FakeReader()
        service = DepositService(_TARIFF, _history(BillingMonth(2026, 1)))

        await ProductionService(reader, service).async_refresh(
            datetime.date(2026, 2, 1)
        )

        assert reader.asked[0] == datetime.date(
            FIRST_MEASURED_MONTH.year, FIRST_MEASURED_MONTH.month, 1
        )


class TestPublishedPrices:
    """Prices scraped from PSE layer over the shipped table, never replace it."""

    MONTH = BillingMonth(2026, 1)

    def _service(self) -> DepositService:
        return DepositService(
            _TARIFF,
            _history(self.MONTH),
            monthly_prices=MonthlyMarketPrices({self.MONTH: 200.0}),
        )

    def _earned_at_monthly_price(self, service: DepositService) -> float | None:
        rows = service.report.to_dict()["history"]
        return next(r for r in rows if r["month"] == str(self.MONTH))[
            "earned_at_monthly_price"
        ]

    def test_a_published_correction_wins_over_the_shipped_price(self):
        service = self._service()

        service.update_market_prices({self.MONTH: 250.0})

        assert self._earned_at_monthly_price(service) == pytest.approx(
            100.0 * 0.25 * 1.23
        )

    def test_the_shipped_table_survives_a_refresh_that_missed_a_month(self):
        """A scraped page is allowed to rot; what it omits must not disappear."""
        service = self._service()

        service.update_market_prices({BillingMonth(2025, 12): 300.0})

        assert self._earned_at_monthly_price(service) == pytest.approx(
            100.0 * 0.2 * 1.23
        )
