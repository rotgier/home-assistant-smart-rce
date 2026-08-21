"""SettlementHistory — watermark, day merging and month roll-up."""

import datetime

from custom_components.smart_rce.deposit.domain.billing_month import BillingMonth
from custom_components.smart_rce.deposit.domain.reference_year import MonthRecord
from custom_components.smart_rce.deposit.domain.settlement_history import (
    DayRecord,
    SettlementHistory,
)
from custom_components.smart_rce.deposit.domain.tariff import Zone
import pytest


def _day(iso: str, *, exported=10.0, earned=5.0, t3=2.0) -> DayRecord:
    return DayRecord(
        day=datetime.date.fromisoformat(iso),
        exported_kwh=exported,
        deposit_earned=earned,
        import_kwh={Zone.T1: 0.0, Zone.T2: 0.0, Zone.T3: t3},
    )


def _month(text: str) -> MonthRecord:
    return MonthRecord(
        month=BillingMonth.parse(text),
        exported_kwh=100.0,
        deposit_earned=50.0,
        import_kwh={Zone.T1: 0.0, Zone.T2: 0.0, Zone.T3: 20.0},
    )


def test_seed_watermark_is_the_last_day_of_the_last_closed_month():
    history = SettlementHistory.from_seed([_month("2026-06"), _month("2026-07")])

    assert history.last_data_day == datetime.date(2026, 7, 31)
    assert history.next_day_to_fetch() == datetime.date(2026, 8, 1)


def test_seed_rejects_empty_history():
    with pytest.raises(ValueError, match="empty"):
        SettlementHistory.from_seed([])


def test_added_days_advance_the_watermark():
    history = SettlementHistory.from_seed([_month("2026-07")])

    history.add_days([_day("2026-08-01"), _day("2026-08-02")])

    assert history.last_data_day == datetime.date(2026, 8, 2)
    assert history.elapsed_days == 2


def test_partial_sums_measured_days_without_extrapolating():
    history = SettlementHistory.from_seed([_month("2026-07")])

    history.add_days(
        [
            _day("2026-08-01", exported=10, earned=5, t3=2),
            _day("2026-08-02", exported=20, earned=7, t3=3),
        ]
    )

    partial = history.partial
    assert partial is not None
    assert partial.month == BillingMonth(2026, 8)
    assert partial.exported_kwh == pytest.approx(30.0)
    assert partial.deposit_earned == pytest.approx(12.0)
    assert partial.import_kwh[Zone.T3] == pytest.approx(5.0)


def test_refetching_a_day_replaces_it_instead_of_doubling():
    history = SettlementHistory.from_seed([_month("2026-07")])
    history.add_days([_day("2026-08-01", exported=10, earned=5)])

    history.add_days([_day("2026-08-01", exported=99, earned=42)])

    assert history.elapsed_days == 1
    assert history.partial.exported_kwh == pytest.approx(99.0)
    assert history.partial.deposit_earned == pytest.approx(42.0)


def test_month_rolls_up_once_a_later_day_arrives():
    history = SettlementHistory.from_seed([_month("2026-07")])
    history.add_days([_day("2026-08-30", earned=5), _day("2026-08-31", earned=6)])

    history.add_days([_day("2026-09-01", earned=1)])

    assert [str(m.month) for m in history.months] == ["2026-07", "2026-08"]
    august = history.months[-1]
    assert august.deposit_earned == pytest.approx(11.0)
    assert history.elapsed_days == 1  # only September remains open
    assert history.partial.month == BillingMonth(2026, 9)


def test_a_missed_run_still_rolls_up_correctly():
    """Days arriving late, spanning two month boundaries, must not be lost."""
    history = SettlementHistory.from_seed([_month("2026-07")])

    history.add_days(
        [
            _day("2026-08-31", earned=3),
            _day("2026-09-01", earned=4),
            _day("2026-10-01", earned=5),
        ]
    )

    assert [str(m.month) for m in history.months] == ["2026-07", "2026-08", "2026-09"]
    assert history.partial.month == BillingMonth(2026, 10)
    assert history.last_data_day == datetime.date(2026, 10, 1)


def test_round_trips_through_storage():
    history = SettlementHistory.from_seed([_month("2026-07")])
    history.add_days([_day("2026-08-01"), _day("2026-08-02")])

    restored = SettlementHistory.from_dict(history.to_dict())

    assert restored.to_dict() == history.to_dict()
    assert restored.last_data_day == history.last_data_day
    assert restored.partial.exported_kwh == pytest.approx(history.partial.exported_kwh)


def test_no_days_means_no_partial():
    history = SettlementHistory.from_seed([_month("2026-07")])

    assert history.partial is None
    assert history.elapsed_days == 0
