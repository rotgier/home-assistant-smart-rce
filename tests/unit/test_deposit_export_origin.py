"""Where the deposit came from: PV or the battery (ADR-028, 25.10.2026)."""

from __future__ import annotations

import asyncio
import datetime

from custom_components.smart_rce.deposit.application.deposit_service import (
    DepositService,
)
from custom_components.smart_rce.deposit.application.export_origin_service import (
    ExportOriginService,
)
from custom_components.smart_rce.deposit.domain.billing_month import BillingMonth
from custom_components.smart_rce.deposit.domain.export_origin import ExportOrigin
from custom_components.smart_rce.deposit.domain.hourly_history import (
    MonthlyHours,
    PricedHour,
)
from custom_components.smart_rce.deposit.domain.reference_year import MonthRecord
from custom_components.smart_rce.deposit.domain.settlement_history import (
    SettlementHistory,
)
from custom_components.smart_rce.deposit.domain.tariff import FlatRates, Tariff
from custom_components.smart_rce.tariff import Zone, ZoneRates
import pytest

OCTOBER = BillingMonth(2026, 10)
SEPTEMBER = BillingMonth(2026, 9)
DAY = datetime.date(2026, 10, 20)


def _hour(exported: float, price: float | None = 1000.0) -> PricedHour:
    return PricedHour(exported_kwh=exported, imported_kwh=0.0, price_pln_mwh=price)


class TestExportOrigin:
    def test_an_evening_sale_is_the_battery(self):
        # 19:00, no sun: 2 kWh out, battery gave 3 (1 to the house) -> 2 from it.
        origin = ExportOrigin.of_month(
            OCTOBER, {DAY: {19: _hour(2.0)}}, {(DAY, 19): 3.0}
        )
        assert (origin.pv_kwh, origin.battery_kwh) == (0.0, 2.0)
        assert origin.battery_pln == pytest.approx(2.0 * 1.0 * 1.23)

    def test_midday_export_without_discharge_is_pv(self):
        origin = ExportOrigin.of_month(OCTOBER, {DAY: {12: _hour(4.0, 300.0)}}, {})
        assert (origin.pv_kwh, origin.battery_kwh) == (4.0, 0.0)
        assert origin.pv_pln == pytest.approx(4.0 * 0.3 * 1.23)

    def test_a_morning_sale_with_some_sun_splits_the_hour(self):
        # 3 kWh out, battery gave 1: PV covered the house and sent 2 of its own.
        origin = ExportOrigin.of_month(OCTOBER, {DAY: {8: _hour(3.0)}}, {(DAY, 8): 1.0})
        assert (origin.pv_kwh, origin.battery_kwh) == (2.0, 1.0)

    def test_an_hour_without_a_price_counts_energy_but_earns_nothing(self):
        origin = ExportOrigin.of_month(
            OCTOBER, {DAY: {19: _hour(2.0, None)}}, {(DAY, 19): 2.0}
        )
        assert (origin.battery_kwh, origin.battery_pln) == (2.0, 0.0)

    def test_before_february_2025_there_is_no_1_23(self):
        january = BillingMonth(2025, 1)
        day = datetime.date(2025, 1, 10)
        origin = ExportOrigin.of_month(
            january, {day: {19: _hour(1.0)}}, {(day, 19): 1.0}
        )
        assert origin.battery_pln == pytest.approx(1.0)


class _Archive:
    def __init__(self, months: dict[BillingMonth, MonthlyHours]) -> None:
        self._months = months

    async def async_month(self, month: BillingMonth) -> MonthlyHours:
        return self._months.get(month, MonthlyHours(month))


class _Reader:
    def __init__(self, discharge: dict) -> None:
        self.discharge = discharge
        self.asked: list[tuple[datetime.date, datetime.date]] = []

    async def async_hours_between(self, start, end):
        self.asked.append((start, end))
        return self.discharge


def _deposit() -> DepositService:
    tariff = Tariff(
        {
            BillingMonth(2026, 1): ZoneRates(
                dict.fromkeys(Zone, 0.5), dict.fromkeys(Zone, 0.1)
            )
        },
        {BillingMonth(2026, 1): FlatRates(energy=0.6, distribution=0.3)},
    )
    history = SettlementHistory(
        [
            MonthRecord(
                month=SEPTEMBER.shifted(offset - 11),
                exported_kwh=100.0,
                deposit_earned=50.0,
                import_kwh=dict.fromkeys(Zone, 10.0),
            )
            for offset in range(12)
        ],
        last_data_day=datetime.date(2026, 9, 30),
    )
    return DepositService(tariff, history)


class TestService:
    def test_the_report_carries_this_and_last_month(self):
        september_day = datetime.date(2026, 9, 14)
        archive = _Archive(
            {
                OCTOBER: MonthlyHours(OCTOBER, {DAY: {19: _hour(2.0)}}),
                SEPTEMBER: MonthlyHours(SEPTEMBER, {september_day: {12: _hour(5.0)}}),
            }
        )
        reader = _Reader({(DAY, 19): 2.0})
        deposit = _deposit()

        asyncio.run(ExportOriginService(archive, reader, deposit).async_refresh(DAY))

        report = deposit.report
        assert report.origin_of(0).battery_kwh == 2.0
        assert report.origin_of(1).pv_kwh == 5.0
        assert reader.asked == [
            (datetime.date(2026, 9, 1), datetime.date(2026, 9, 30)),
            (datetime.date(2026, 10, 1), datetime.date(2026, 10, 31)),
        ]

    def test_no_split_yet_reads_as_unknown(self):
        assert _deposit().report.origin_of(0) is None
