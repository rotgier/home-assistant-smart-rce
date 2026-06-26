"""Unit tests for ChargeSlots.compute — fixed-length override vs auto path."""

from datetime import date

from custom_components.smart_rce.domain.charge_slots import ChargeSlots
from custom_components.smart_rce.domain.rce import RceDayPrices


def _day(prices: list[float]) -> RceDayPrices:
    return RceDayPrices(
        published_at=None, day=date(2026, 6, 26), hour_price=tuple(prices)
    )


def _flat_with_valley(valley_start: int, length: int) -> list[float]:
    """24 hourly prices, all 100 except a cheap `length`-hour valley at 10."""
    prices = [100.0] * 24
    for h in range(valley_start, valley_start + length):
        prices[h] = 10.0
    return prices


class TestForcedWindow:
    def test_override_picks_cheapest_two_hour_window(self):
        # Cheapest consecutive 2h sits at 10:00-12:00.
        window = ChargeSlots.compute(
            _day(_flat_with_valley(10, 2)), charge_hours_override=2
        )
        assert window is not None
        assert window.start_hour == 10.0
        assert window.end_hour == 12.0

    def test_override_window_length_matches_n(self):
        for n in (2, 3, 5, 8):
            window = ChargeSlots.compute(
                _day(_flat_with_valley(7, n)), charge_hours_override=n
            )
            assert window is not None
            assert window.end_hour - window.start_hour == n

    def test_override_start_is_integer_no_half_hour_shift(self):
        # Auto path applies a -0.5 shift for N=3; override must NOT.
        window = ChargeSlots.compute(
            _day(_flat_with_valley(9, 3)), charge_hours_override=3
        )
        assert window is not None
        assert window.start_hour.is_integer()

    def test_override_start_searched_within_6_16(self):
        # A cheaper nighttime valley (02:00) is ignored — start stays in 6..15.
        prices = [100.0] * 24
        prices[2] = prices[3] = 1.0  # nighttime, outside search window
        prices[11] = prices[12] = 10.0  # daytime valley
        window = ChargeSlots.compute(_day(prices), charge_hours_override=2)
        assert window is not None
        assert 6 <= window.start_hour <= 15


class TestAutoPathUnchanged:
    def test_none_equals_default_auto(self):
        day = _day(_flat_with_valley(10, 4))
        assert ChargeSlots.compute(
            day, charge_hours_override=None
        ) == ChargeSlots.compute(day)

    def test_empty_day_returns_none(self):
        assert ChargeSlots.compute(None, charge_hours_override=2) is None
        assert ChargeSlots.compute(_day([]), charge_hours_override=2) is None
