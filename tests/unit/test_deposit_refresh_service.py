"""DepositRefreshService — watermark handling and the two safety guards."""

import datetime

from custom_components.smart_rce.deposit.application.refresh_service import (
    DepositRefreshService,
)
from custom_components.smart_rce.deposit.domain.billing_month import BillingMonth
from custom_components.smart_rce.deposit.domain.meter_reading import HourReading
from custom_components.smart_rce.deposit.domain.reference_year import MonthRecord
from custom_components.smart_rce.deposit.domain.settlement_history import (
    SettlementHistory,
)
from custom_components.smart_rce.deposit.domain.tariff import Zone
import pytest


class _FakeRepository:
    def __init__(self, history: SettlementHistory) -> None:
        self.history = history
        self.persisted = 0

    async def persist(self) -> None:
        self.persisted += 1


class _FakeMeter:
    """Records its calls — one login per run is the whole point of the contract."""

    def __init__(self, days: list[datetime.date]) -> None:
        self._days = days
        self.calls: list[tuple[datetime.date, datetime.date]] = []

    async def async_readings_for(self, start, end):
        self.calls.append((start, end))
        return {
            day: {12: HourReading(exported_kwh=1.0, imported_kwh=0.0)}
            for day in self._days
            if start <= day <= end
        }


class _FakePrices:
    def __init__(self, missing: set[datetime.date] | None = None) -> None:
        self._missing = missing or set()
        self.asked: list[datetime.date] = []

    async def async_prices_for(self, day):
        self.asked.append(day)
        return None if day in self._missing else {12: 500.0}


def _history() -> SettlementHistory:
    return SettlementHistory.from_seed(
        [
            MonthRecord(
                month=BillingMonth(2026, 7),
                exported_kwh=100.0,
                deposit_earned=50.0,
                import_kwh={Zone.T1: 0.0, Zone.T2: 0.0, Zone.T3: 10.0},
            )
        ]
    )


def _service(repo, meter, prices, updates: list[int]):
    return DepositRefreshService(
        repo, prices, meter, on_updated=lambda: updates.append(1)
    )


async def test_fetches_everything_between_the_watermark_and_yesterday():
    repo = _FakeRepository(_history())
    days = [datetime.date(2026, 8, d) for d in (1, 2, 3)]
    meter, prices, updates = _FakeMeter(days), _FakePrices(), []

    added = await _service(repo, meter, prices, updates).async_refresh(
        datetime.date(2026, 8, 4)
    )

    assert added == 3
    assert meter.calls == [(datetime.date(2026, 8, 1), datetime.date(2026, 8, 3))]
    assert repo.history.last_data_day == datetime.date(2026, 8, 3)
    assert repo.persisted == 1
    assert updates == [1]


async def test_asks_the_meter_once_for_the_whole_range():
    """TAURON bans on a burst of logins, so a per-day loop would be a real bug."""
    repo = _FakeRepository(_history())
    days = [datetime.date(2026, 8, d) for d in range(1, 11)]
    meter = _FakeMeter(days)

    await _service(repo, meter, _FakePrices(), []).async_refresh(
        datetime.date(2026, 8, 11)
    )

    assert len(meter.calls) == 1


async def test_does_nothing_when_the_watermark_is_current():
    repo = _FakeRepository(_history())
    meter, updates = _FakeMeter([]), []

    added = await _service(repo, meter, _FakePrices(), updates).async_refresh(
        datetime.date(2026, 8, 1)
    )

    assert added == 0
    assert meter.calls == []
    assert repo.persisted == 0
    assert updates == []


async def test_stops_at_the_first_day_without_prices_instead_of_skipping_it():
    """Skipping would push the watermark past a day that was never valued."""
    repo = _FakeRepository(_history())
    days = [datetime.date(2026, 8, d) for d in (1, 2, 3)]
    meter = _FakeMeter(days)
    prices = _FakePrices(missing={datetime.date(2026, 8, 2)})

    added = await _service(repo, meter, prices, []).async_refresh(
        datetime.date(2026, 8, 4)
    )

    assert added == 1
    assert repo.history.last_data_day == datetime.date(2026, 8, 1)
    assert datetime.date(2026, 8, 3) not in prices.asked


async def test_a_long_outage_is_caught_up_in_chunks():
    repo = _FakeRepository(_history())
    days = [datetime.date(2026, 8, 1) + datetime.timedelta(d) for d in range(120)]
    meter = _FakeMeter(days)

    await _service(repo, meter, _FakePrices(), []).async_refresh(
        datetime.date(2026, 12, 1)
    )

    start, end = meter.calls[0]
    assert start == datetime.date(2026, 8, 1)
    assert (end - start).days + 1 == DepositRefreshService._MAX_DAYS_PER_RUN


async def test_reported_days_are_valued_with_their_own_prices():
    repo = _FakeRepository(_history())
    meter = _FakeMeter([datetime.date(2026, 8, 1)])

    await _service(repo, meter, _FakePrices(), []).async_refresh(
        datetime.date(2026, 8, 2)
    )

    partial = repo.history.partial
    assert partial is not None
    assert partial.deposit_earned == pytest.approx(500.0 / 1000 * 1.23)
