"""DepositRefreshService — watermark handling and the guards around it."""

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


# A settled day always shows some import: a house draws something overnight even
# when the sun paid for the afternoon. Zero across the board means TAURON has not
# balanced the day yet — see `is_balanced`.
_SETTLED_DAY = {
    12: HourReading(exported_kwh=1.0, imported_kwh=0.0),
    3: HourReading(exported_kwh=0.0, imported_kwh=0.5),
}
_UNBALANCED_DAY = {hour: HourReading(0.0, 0.0) for hour in range(24)}


class _FakeMeter:
    """Records its calls — one login per run is the whole point of the contract."""

    def __init__(
        self,
        days: list[datetime.date],
        unbalanced: set[datetime.date] | None = None,
    ) -> None:
        self._days = days
        self._unbalanced = unbalanced or set()
        self.calls: list[tuple[datetime.date, datetime.date]] = []

    async def async_readings_for(self, start, end):
        self.calls.append((start, end))
        return {
            day: _UNBALANCED_DAY if day in self._unbalanced else _SETTLED_DAY
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


def _at(year: int, month: int, day: int, hour: int = 12) -> datetime.datetime:
    """Return a moment on that day — rationing looks at time, not just date."""
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.UTC)


class _FakeArchive:
    """Collects what was handed over, so tests can assert the hours were kept."""

    def __init__(self) -> None:
        self.days: dict = {}

    async def async_record(self, days) -> None:
        self.days.update(days)


def _service(repo, meter, prices, updates: list[int], archive=None):
    return DepositRefreshService(
        repo,
        prices,
        meter,
        archive or _FakeArchive(),
        on_updated=lambda: updates.append(1),
    )


async def test_fetches_everything_between_the_watermark_and_yesterday():
    repo = _FakeRepository(_history())
    days = [datetime.date(2026, 8, d) for d in (1, 2, 3)]
    meter, prices, updates = _FakeMeter(days), _FakePrices(), []

    added = await _service(repo, meter, prices, updates).async_refresh(_at(2026, 8, 4))

    assert added == 3
    assert meter.calls[0][1] == datetime.date(2026, 8, 3)
    assert repo.history.last_data_day == datetime.date(2026, 8, 3)
    assert repo.persisted == 1
    assert updates == [1]


async def test_asks_the_meter_once_for_the_whole_range():
    """TAURON bans on a burst of logins, so a per-day loop would be a real bug."""
    repo = _FakeRepository(_history())
    days = [datetime.date(2026, 8, d) for d in range(1, 11)]
    meter = _FakeMeter(days)

    await _service(repo, meter, _FakePrices(), []).async_refresh(_at(2026, 8, 11))

    assert len(meter.calls) == 1


async def test_the_meter_is_read_once_a_day_however_often_the_run_fires():
    """The run also fires on every reload, and TAURON bans on a burst of logins."""
    repo = _FakeRepository(_history())
    days = [datetime.date(2026, 8, d) for d in (1, 2, 3)]
    meter = _FakeMeter(days)
    service = _service(repo, meter, _FakePrices(), [])

    await service.async_refresh(_at(2026, 8, 4))
    added = await service.async_refresh(_at(2026, 8, 4))

    assert added == 0
    assert len(meter.calls) == 1


async def test_a_failed_call_does_not_burn_the_day_s_attempt():
    """Otherwise one outage at 04:15 would cost a whole day of data."""
    repo = _FakeRepository(_history())
    meter = _FakeMeter([])

    await _service(repo, meter, _FakePrices(), []).async_refresh(_at(2026, 8, 4))

    assert repo.history.last_meter_call == _at(2026, 8, 4)


async def test_a_day_the_meter_has_not_balanced_yet_is_not_settled():
    """The bug this exists for: TAURON serves 24 empty hours for a fresh day.

    Stored as-is they are indistinguishable from a real day of doing nothing, and
    permanent — the watermark moves past and nothing fetches that day again. Four
    of six days sat in the store like that on 2026-08-27.
    """
    repo = _FakeRepository(_history())
    days = [datetime.date(2026, 8, d) for d in (1, 2)]
    meter = _FakeMeter(days, unbalanced={datetime.date(2026, 8, 2)})

    added = await _service(repo, meter, _FakePrices(), []).async_refresh(
        _at(2026, 8, 3)
    )

    assert added == 1
    assert repo.history.last_data_day == datetime.date(2026, 8, 1)


async def test_the_trailing_week_is_re_read_so_late_data_can_land():
    """TAURON fills a day in over the following days; the first answer is not final."""
    repo = _FakeRepository(_history())
    days = [datetime.date(2026, 8, d) for d in range(1, 9)]
    meter = _FakeMeter(days)

    await _service(repo, meter, _FakePrices(), []).async_refresh(_at(2026, 8, 9))
    repo.history.mark_meter_called(_at(2026, 8, 1))  # allow a second run
    await _service(repo, meter, _FakePrices(), []).async_refresh(_at(2026, 8, 9))

    start, end = meter.calls[-1]
    assert (end - start).days + 1 == DepositRefreshService._TRAILING_DAYS


async def test_a_re_read_that_is_still_unbalanced_leaves_the_stored_day_alone():
    """A day already past the watermark must never be replaced by a placeholder."""
    repo = _FakeRepository(_history())
    settled = datetime.date(2026, 8, 1)
    meter = _FakeMeter([settled])

    await _service(repo, meter, _FakePrices(), []).async_refresh(_at(2026, 8, 2))
    before = repo.history.partial
    repo.history.mark_meter_called(_at(2026, 7, 1))
    meter = _FakeMeter([settled], unbalanced={settled})
    added = await _service(repo, meter, _FakePrices(), []).async_refresh(
        _at(2026, 8, 2)
    )

    assert added == 0
    assert before is not None
    assert repo.history.partial == before


async def test_stops_at_the_first_day_without_prices_instead_of_skipping_it():
    """Skipping would push the watermark past a day that was never valued."""
    repo = _FakeRepository(_history())
    days = [datetime.date(2026, 8, d) for d in (1, 2, 3)]
    meter = _FakeMeter(days)
    prices = _FakePrices(missing={datetime.date(2026, 8, 2)})

    added = await _service(repo, meter, prices, []).async_refresh(_at(2026, 8, 4))

    assert added == 1
    assert repo.history.last_data_day == datetime.date(2026, 8, 1)
    assert datetime.date(2026, 8, 3) not in prices.asked


async def test_a_long_outage_is_caught_up_in_chunks():
    repo = _FakeRepository(_history())
    days = [datetime.date(2026, 8, 1) + datetime.timedelta(d) for d in range(120)]
    meter = _FakeMeter(days)

    await _service(repo, meter, _FakePrices(), []).async_refresh(_at(2026, 12, 1))

    start, end = meter.calls[0]
    assert start == datetime.date(2026, 8, 1)
    assert (end - start).days + 1 == DepositRefreshService._MAX_DAYS_PER_RUN


async def test_reported_days_are_valued_with_their_own_prices():
    repo = _FakeRepository(_history())
    meter = _FakeMeter([datetime.date(2026, 8, 1)])

    await _service(repo, meter, _FakePrices(), []).async_refresh(_at(2026, 8, 2))

    partial = repo.history.partial
    assert partial is not None
    assert partial.deposit_earned == pytest.approx(500.0 / 1000 * 1.23)


async def test_a_second_attempt_is_allowed_while_yesterday_is_still_missing():
    """The meter publishes the day at an hour nobody states — worth chasing."""
    repo = _FakeRepository(_history())
    day = datetime.date(2026, 8, 1)
    unready = _FakeMeter([day], unbalanced={day})
    service = _service(repo, unready, _FakePrices(), [])

    await service.async_refresh(_at(2026, 8, 2, hour=12))
    ready = _FakeMeter([day])
    added = await _service(repo, ready, _FakePrices(), []).async_refresh(
        _at(2026, 8, 2, hour=17)
    )

    assert added == 1
    assert repo.history.last_data_day == day


async def test_a_retry_too_soon_is_refused():
    """Reloads must not turn a missing day into a burst of logins."""
    repo = _FakeRepository(_history())
    day = datetime.date(2026, 8, 1)
    meter = _FakeMeter([day], unbalanced={day})
    service = _service(repo, meter, _FakePrices(), [])

    await service.async_refresh(_at(2026, 8, 2, hour=12))
    await service.async_refresh(_at(2026, 8, 2, hour=13))

    assert len(meter.calls) == 1


async def test_a_stored_day_of_nothing_but_zeros_is_flagged():
    """The alarm that was missing: `last_data_day` alone said all was well."""
    repo = _FakeRepository(_history())
    day = datetime.date(2026, 8, 1)
    meter = _FakeMeter([day])

    await _service(repo, meter, _FakePrices(), []).async_refresh(_at(2026, 8, 2))

    assert repo.history.unsettled_days == ()


async def test_the_hours_behind_a_settled_day_are_archived():
    """Same pass as the valuation — asking for them again would mean another login."""
    repo = _FakeRepository(_history())
    day = datetime.date(2026, 8, 1)
    archive = _FakeArchive()

    await _service(repo, _FakeMeter([day]), _FakePrices(), [], archive).async_refresh(
        _at(2026, 8, 2)
    )

    assert archive.days[day][12].exported_kwh == pytest.approx(1.0)
    assert archive.days[day][12].price_pln_mwh == pytest.approx(500.0)


async def test_a_day_that_was_not_settled_is_not_archived_either():
    """The archive must not fill up with placeholder days the ledger refused."""
    repo = _FakeRepository(_history())
    day = datetime.date(2026, 8, 1)
    archive = _FakeArchive()

    await _service(
        repo, _FakeMeter([day], unbalanced={day}), _FakePrices(), [], archive
    ).async_refresh(_at(2026, 8, 2))

    assert archive.days == {}
