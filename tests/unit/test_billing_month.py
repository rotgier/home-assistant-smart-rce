"""BillingMonth arithmetic — the month-boundary edge cases the projection relies on."""

from custom_components.smart_rce.deposit.domain.billing_month import BillingMonth
import pytest


def test_parses_storage_form():
    assert BillingMonth.parse("2026-07") == BillingMonth(2026, 7)


def test_str_round_trips_through_parse():
    month = BillingMonth(2026, 1)
    assert BillingMonth.parse(str(month)) == month


@pytest.mark.parametrize(
    ("start", "shift", "expected"),
    [
        (BillingMonth(2026, 12), 1, BillingMonth(2027, 1)),
        (BillingMonth(2026, 1), -1, BillingMonth(2025, 12)),
        (BillingMonth(2026, 7), 12, BillingMonth(2027, 7)),
        (BillingMonth(2026, 7), 0, BillingMonth(2026, 7)),
        (BillingMonth(2026, 3), -14, BillingMonth(2025, 1)),
    ],
)
def test_shifted_crosses_year_boundaries(start, shift, expected):
    assert start.shifted(shift) == expected


def test_months_since_is_signed():
    assert BillingMonth(2027, 8).months_since(BillingMonth(2026, 8)) == 12
    assert BillingMonth(2026, 8).months_since(BillingMonth(2027, 8)) == -12


def test_ordering_follows_the_calendar():
    assert BillingMonth(2025, 12) < BillingMonth(2026, 1)
    assert max([BillingMonth(2026, 2), BillingMonth(2026, 11)]) == BillingMonth(
        2026, 11
    )


def test_days_handles_leap_february():
    assert BillingMonth(2024, 2).days == 29
    assert BillingMonth(2026, 2).days == 28
    assert BillingMonth(2026, 8).days == 31
