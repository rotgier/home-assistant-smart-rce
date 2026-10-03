"""Building the current month for the reference year: real days plus last year's.

The projection needs whole months, and the current one is half-written. Until
2026-10-03 the measured part was simply scaled up to a full month, which turned
two sunny days of October into 676 kWh — three times the real October before it —
and took the winter-trough sensor with it.
"""

import datetime

from custom_components.smart_rce.deposit.application.deposit_service import (
    DepositService,
)
from custom_components.smart_rce.deposit.domain.billing_month import BillingMonth
from custom_components.smart_rce.deposit.domain.reference_year import MonthRecord
from custom_components.smart_rce.deposit.domain.settlement_history import (
    DayRecord,
    SettlementHistory,
)
from custom_components.smart_rce.deposit.domain.tariff import FlatRates, Tariff
from custom_components.smart_rce.tariff import Zone, ZoneRates
import pytest

_TARIFF = Tariff(
    {
        BillingMonth(2026, 1): ZoneRates(
            dict.fromkeys(Zone, 0.5), dict.fromkeys(Zone, 0.1)
        )
    },
    {BillingMonth(2026, 1): FlatRates(energy=0.6, distribution=0.3)},
)
_OCTOBER = BillingMonth(2026, 10)


def _day(date: datetime.date, exported: float, earned: float) -> DayRecord:
    return DayRecord(
        day=date,
        exported_kwh=exported,
        deposit_earned=earned,
        import_kwh=dict.fromkeys(Zone, 1.0),
    )


def _history(*measured: DayRecord) -> SettlementHistory:
    """Twelve closed months ending with September, plus the given October days."""
    records = [
        MonthRecord(
            month=BillingMonth(2025, 10).shifted(offset),
            exported_kwh=100.0,
            deposit_earned=50.0,
            import_kwh=dict.fromkeys(Zone, 10.0),
        )
        for offset in range(12)
    ]
    history = SettlementHistory(records, last_data_day=datetime.date(2026, 9, 30))
    history.add_days(measured)
    return history


def _last_year(exported: float, earned: float) -> dict[datetime.date, DayRecord]:
    """Every day of October, as it supposedly was a year ago."""
    return {
        datetime.date(2026, 10, number): _day(
            datetime.date(2026, 10, number), exported, earned
        )
        for number in range(1, 32)
    }


def _reference_october(service: DepositService):
    """October as the projection sees it — the first month it has to forecast."""
    first = service.report.winter.months[0]
    assert str(first.month) == "2026-10"
    return first


class TestComposition:
    def test_days_that_happened_come_from_this_year(self):
        service = DepositService(
            _TARIFF, _history(_day(datetime.date(2026, 10, 1), 20.0, 18.0))
        )
        service.update_last_year_days(_last_year(exported=5.0, earned=3.0))

        october = _reference_october(service)

        assert october.month == _OCTOBER
        # day 1 real (20 kWh), days 2-31 from last year (30 x 5 kWh)
        assert october.exported_kwh == pytest.approx(20.0 + 30 * 5.0)

    def test_the_month_is_not_scaled_up_from_the_days_so_far(self):
        """Two good days must not become a record-breaking month."""
        service = DepositService(
            _TARIFF,
            _history(
                _day(datetime.date(2026, 10, 1), 22.0, 15.0),
                _day(datetime.date(2026, 10, 2), 22.0, 15.0),
            ),
        )
        service.update_last_year_days(_last_year(exported=5.0, earned=3.0))

        october = _reference_october(service)

        assert october.exported_kwh == pytest.approx(44.0 + 29 * 5.0)
        assert october.exported_kwh < 22.0 * 31  # what extrapolation would have said

    def test_it_grows_as_better_days_replace_last_year_s(self):
        """The property the user predicted: each real day displaces a weaker one.

        This year exports more than last, so every day that happens raises the
        month — and the projection drifts up through the month instead of
        swinging on the first days' weather.
        """
        totals = []
        for last_day in (1, 5, 10):
            measured = [
                _day(datetime.date(2026, 10, number), 20.0, 18.0)
                for number in range(1, last_day + 1)
            ]
            service = DepositService(_TARIFF, _history(*measured))
            service.update_last_year_days(_last_year(exported=5.0, earned=3.0))
            totals.append(_reference_october(service).exported_kwh)

        assert totals == sorted(totals)
        assert totals[0] < totals[-1]

    def test_without_last_year_only_the_measured_days_count(self):
        """No archive for that month — better a short month than an invented one."""
        service = DepositService(
            _TARIFF, _history(_day(datetime.date(2026, 10, 1), 20.0, 18.0))
        )

        october = _reference_october(service)

        assert october.exported_kwh == pytest.approx(20.0)
