"""Tests for discharge_slots domain logic (best_morning_discharge_slot, max_upcoming_peak)."""

from datetime import date, datetime

from custom_components.smart_rce.domain.discharge_slots import (
    MORNING_DISCHARGE_TIE_BREAK_TOLERANCE_PLN_MWH_GROSS,
    best_morning_discharge_slot,
)
from custom_components.smart_rce.domain.rce import TIMEZONE, RceData, RceDayPrices
import pytest


def _hour_price(slots: list[tuple[int, float]]) -> tuple[float, ...]:
    """Convert list of (hour, price) pairs into 24-element hour_price tuple (zeros elsewhere)."""
    prices = [0.0] * 24
    for hour, price in slots:
        prices[hour] = price
    return tuple(prices)


def _data(
    today_slots: list[tuple[int, float]],
    tomorrow_slots: list[tuple[int, float]] | None = None,
    today_day: int = 16,
    tomorrow_day: int = 17,
) -> RceData:
    today = RceDayPrices(
        published_at=datetime(2026, 4, 15, 14, 0, tzinfo=TIMEZONE),
        day=date(2026, 4, today_day),
        hour_price=_hour_price(today_slots),
    )
    tomorrow = (
        RceDayPrices(
            published_at=datetime(2026, 4, 16, 14, 0, tzinfo=TIMEZONE),
            day=date(2026, 4, tomorrow_day),
            hour_price=_hour_price(tomorrow_slots),
        )
        if tomorrow_slots is not None
        else None
    )
    return RceData(
        fetched_at=datetime(2026, 4, 16, 0, 0, tzinfo=TIMEZONE),
        today=today,
        tomorrow=tomorrow,
    )


@pytest.fixture
def now_midnight() -> datetime:
    """4/16 00:00 — przed całym morning window."""
    return datetime(2026, 4, 16, 0, 0, tzinfo=TIMEZONE)


def test_picks_single_peak_when_others_far_below(now_midnight):
    """Brak near-peak alternatyw → wybiera najwyższy slot, niezależnie od czasu."""
    data = _data([(5, 400), (6, 600), (7, 400)])
    best = best_morning_discharge_slot(data, now_midnight)
    assert best is not None
    assert best.datetime.hour == 6
    assert best.price == 600


def test_picks_latest_when_all_within_tolerance(now_midnight):
    """Wszystkie sloty w tolerancji od peaku → wybiera najpóźniejszy."""
    data = _data([(5, 500), (6, 510), (7, 505)])
    best = best_morning_discharge_slot(data, now_midnight)
    assert best is not None
    assert best.datetime.hour == 7
    assert best.price == 505


def test_excludes_slot_outside_tolerance(now_midnight):
    """Slot z różnicą > tolerancja → odrzucony, mimo że późniejszy."""
    tol_net = MORNING_DISCHARGE_TIE_BREAK_TOLERANCE_PLN_MWH_GROSS / 1.23
    peak = 600.0
    just_outside = peak - tol_net - 1.0  # 1 PLN/MWh poza tolerance
    data = _data([(5, just_outside), (6, peak), (7, just_outside)])
    best = best_morning_discharge_slot(data, now_midnight)
    assert best is not None
    assert best.datetime.hour == 6  # peak wins, 7:00 nie kwalifikuje się


def test_picks_latest_when_all_equal(now_midnight):
    """Identyczne ceny — original tie-break (najpóźniejszy) zachowany."""
    data = _data([(5, 500), (6, 500), (7, 500)])
    best = best_morning_discharge_slot(data, now_midnight)
    assert best is not None
    assert best.datetime.hour == 7


def test_picks_latest_within_tolerance_when_peak_is_earlier(now_midnight):
    """Peak rano, ale późniejszy slot w tolerancji → wybiera późniejszy."""
    tol_net = MORNING_DISCHARGE_TIE_BREAK_TOLERANCE_PLN_MWH_GROSS / 1.23
    inside = 600.0 - tol_net + 0.5  # tuż w tolerancji
    data = _data([(5, 600), (6, inside), (7, 400)])
    best = best_morning_discharge_slot(data, now_midnight)
    assert best is not None
    assert best.datetime.hour == 6


def test_returns_none_when_all_slots_past(now_midnight):
    """Now > MORNING_DISCHARGE_END_HOUR i tomorrow=None → None."""
    data = _data([(5, 500), (6, 510), (7, 505)])
    after_window = datetime(2026, 4, 16, 9, 0, tzinfo=TIMEZONE)
    assert best_morning_discharge_slot(data, after_window) is None


def test_uses_tomorrow_when_today_window_past(now_midnight):
    """Po dzisiejszym oknie → szuka w tomorrow."""
    today_slots = [(5, 500), (6, 510)]
    tomorrow_slots = [(5, 700), (6, 720), (7, 715)]
    data = _data(today_slots, tomorrow_slots)
    after_window = datetime(2026, 4, 16, 12, 0, tzinfo=TIMEZONE)
    best = best_morning_discharge_slot(data, after_window)
    assert best is not None
    assert best.datetime.day == 17
    # tomorrow: 700, 720, 715 — peak 720, tolerance ~16.26, 715 i 700 → 715 in, 700 out
    # near_peak = [720@6, 715@7] → latest = 7:00
    assert best.datetime.hour == 7
