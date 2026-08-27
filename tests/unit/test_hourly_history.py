"""Hourly detail — kept so a settled day can be re-read without the meter."""

import datetime

from custom_components.smart_rce.deposit.domain.billing_month import BillingMonth
from custom_components.smart_rce.deposit.domain.hourly_history import (
    MonthlyHours,
    PricedHour,
)
import pytest

_MONTH = BillingMonth(2026, 8)
_DAY = datetime.date(2026, 8, 1)
_HOURS = {
    19: PricedHour(exported_kwh=2.5, imported_kwh=0.0, price_pln_mwh=812.3),
    3: PricedHour(exported_kwh=0.0, imported_kwh=0.4, price_pln_mwh=241.0),
}


def _stored() -> MonthlyHours:
    month = MonthlyHours(_MONTH)
    month.record(_DAY, _HOURS)
    return month


def test_a_stored_day_survives_the_round_trip():
    restored = MonthlyHours.from_dict(_MONTH, _stored().to_dict())

    assert restored.days[_DAY][19] == _HOURS[19]


def test_hours_serialise_as_three_numbers_not_three_field_names():
    """The whole point of a per-month file is that it stays small."""
    stored = _stored().to_dict()["days"]["2026-08-01"]

    assert stored["19"] == [2.5, 0.0, 812.3]


def test_an_hour_without_a_published_price_keeps_its_gap():
    """Before July 2024 there is no hourly price — zero would be a wrong number."""
    month = MonthlyHours(_MONTH)
    month.record(_DAY, {5: PricedHour(1.0, 0.0, None)})

    assert (
        MonthlyHours.from_dict(_MONTH, month.to_dict()).days[_DAY][5].price_pln_mwh
        is None
    )


def test_re_reading_a_day_replaces_it():
    """The meter fills days in over the following days; the newer read wins."""
    month = _stored()

    month.record(_DAY, {19: PricedHour(9.9, 0.0, 100.0)})

    assert month.days[_DAY][19].exported_kwh == pytest.approx(9.9)
    assert len(month.days[_DAY]) == 1


def test_a_day_from_another_month_is_refused():
    """Each file holds exactly its own month, or the archive quietly overlaps."""
    with pytest.raises(ValueError, match="does not belong"):
        MonthlyHours(_MONTH).record(datetime.date(2026, 9, 1), _HOURS)
