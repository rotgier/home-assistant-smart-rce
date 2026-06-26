"""Unit tests for ChargeSlots.compute — parametrized base + extend algorithm."""

from datetime import date

from custom_components.smart_rce.domain.charge_slots import (
    ChargeSlots,
    ChargeWindowParams,
)
from custom_components.smart_rce.domain.rce import RceDayPrices


def _day(prices: list[float]) -> RceDayPrices:
    return RceDayPrices(
        published_at=None, day=date(2026, 6, 26), hour_price=tuple(prices)
    )


class TestBaseWindow:
    def test_stays_at_base_when_earlier_hours_expensive(self):
        # Cheapest 2h at 12-14; everything earlier is far pricier → no extend.
        prices = [500.0] * 24
        prices[12] = prices[13] = 10.0
        params = ChargeWindowParams(initial_hours=2, base_window_shift_minutes=0)
        window = ChargeSlots.compute(_day(prices), params=params)
        assert window is not None
        assert window.start_hour == 12.0
        assert window.end_hour == 14.0

    def test_base_window_shift_applies_when_window_equals_base(self):
        # initial=3, no extend → window == base → start shifted 30 min earlier.
        prices = [500.0] * 24
        prices[12] = prices[13] = prices[14] = 10.0
        params = ChargeWindowParams(initial_hours=3, base_window_shift_minutes=30)
        window = ChargeSlots.compute(_day(prices), params=params)
        assert window is not None
        assert window.start_hour == 11.5  # 12:00 - 30 min
        assert window.end_hour == 15.0

    def test_shift_zero_gives_integer_start(self):
        prices = [500.0] * 24
        prices[12] = prices[13] = prices[14] = 10.0
        params = ChargeWindowParams(initial_hours=3, base_window_shift_minutes=0)
        window = ChargeSlots.compute(_day(prices), params=params)
        assert window is not None
        assert window.start_hour == 12.0


class TestExtendEarlier:
    def test_extends_earlier_when_prior_hour_marginally_pricier(self):
        # Base 2h at 12-14 (200). Hour 11 = 240 → 40 above base max (200),
        # below extend_threshold 45 → take earlier 3h window [11-14).
        prices = [500.0] * 24
        prices[12] = prices[13] = 200.0
        prices[11] = 240.0
        params = ChargeWindowParams(
            initial_hours=2,
            extend_threshold=45,
            absolute_cheap_price=100,  # 240 not "cheap" → only threshold path
            base_window_shift_minutes=0,
        )
        window = ChargeSlots.compute(_day(prices), params=params)
        assert window is not None
        assert window.start_hour == 11.0
        assert window.end_hour == 14.0

    def test_no_extend_when_above_threshold(self):
        # Same shape, but threshold 30 < 40 gap → stay at base 2h.
        prices = [500.0] * 24
        prices[12] = prices[13] = 200.0
        prices[11] = 240.0
        params = ChargeWindowParams(
            initial_hours=2,
            extend_threshold=30,
            absolute_cheap_price=100,
            base_window_shift_minutes=0,
        )
        window = ChargeSlots.compute(_day(prices), params=params)
        assert window is not None
        assert window.start_hour == 12.0
        assert window.end_hour == 14.0

    def test_extends_earlier_when_prior_hour_absolutely_cheap(self):
        # Hour 11 = 80 < absolute_cheap 100 → extend even with threshold 0.
        prices = [500.0] * 24
        prices[12] = prices[13] = 200.0
        prices[11] = 80.0
        params = ChargeWindowParams(
            initial_hours=2,
            extend_threshold=0,
            absolute_cheap_price=100,
            base_window_shift_minutes=0,
        )
        window = ChargeSlots.compute(_day(prices), params=params)
        assert window is not None
        assert window.start_hour == 11.0


class TestDefaultsAndGuards:
    def test_none_params_equals_default_params(self):
        prices = [500.0] * 24
        prices[12] = prices[13] = prices[14] = 10.0
        day = _day(prices)
        assert ChargeSlots.compute(day) == ChargeSlots.compute(
            day, params=ChargeWindowParams()
        )

    def test_empty_day_returns_none(self):
        assert ChargeSlots.compute(None, params=ChargeWindowParams()) is None
        assert ChargeSlots.compute(_day([]), params=ChargeWindowParams()) is None
