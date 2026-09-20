"""Evening discharge proposal — the rules the user used to apply by hand.

Cases are named after the situations that actually occur: a single expensive
hour, a pair, a pair followed by a third, a dip in between, and the evening
that loses to the next morning. Prices in these tests are NET (as RCE
publishes them); the proposer converts to gross before comparing.
"""

from datetime import date, time

from custom_components.smart_rce.domain.battery_schedule import SlotBehavior, SlotKind
from custom_components.smart_rce.domain.discharge_proposer import (
    EXPORT_THRESHOLD,
    FULL_TARGET_SOC,
    PARTIAL_TARGET_SOC,
    propose_evening_discharge,
)
from custom_components.smart_rce.domain.rce import RceDayPrices

WORKDAY = date(2026, 9, 21)  # Monday, summer T2 = 19-22
WINTER_WORKDAY = date(2026, 10, 5)  # Monday, winter T2 = 16-21
WEEKEND = date(2026, 9, 20)  # Sunday — T3 all day

# Gross threshold is 750; /1.23 puts the net break-even just under 610.
RICH = 700.0  # 861 gross — qualifies
POOR = 500.0  # 615 gross — does not
CHEAP_MORNING = 100.0


def _day(day: date, **hours: float) -> RceDayPrices:
    prices = [POOR] * 24
    for hour, price in hours.items():
        prices[int(hour[1:])] = price
    return RceDayPrices(published_at=None, day=day, hour_price=tuple(prices))


def _morning(price: float) -> RceDayPrices:
    prices = [CHEAP_MORNING] * 24
    for hour in range(5, 9):
        prices[hour] = price
    return RceDayPrices(
        published_at=None, day=date(2026, 9, 22), hour_price=tuple(prices)
    )


def _propose(today, tomorrow=None, *, day=WORKDAY, is_workday=True):
    return propose_evening_discharge(
        today=today, tomorrow=tomorrow, day=day, is_workday=is_workday
    )


# ─── the three shapes the user described ───


def test_one_expensive_hour_gives_one_window_that_empties_what_it_can():
    proposal = _propose(_day(WORKDAY, h19=RICH), _morning(CHEAP_MORNING))

    assert len(proposal.slots) == 1
    slot = proposal.slots[0]
    assert slot.kind is SlotKind.DISCHARGE_EVENING_EARLY
    assert (slot.start, slot.end) == (time(19, 0), time(20, 0))
    # A single hour cannot reach the target from a full battery anyway.
    assert slot.target_soc == FULL_TARGET_SOC


def test_two_expensive_hours_hold_back_for_the_remaining_t2_hour():
    # 19 and 20 qualify, 21 does not — but 21 is still inside T2, so the house
    # must be able to run off the battery rather than buy at the T2 rate.
    proposal = _propose(_day(WORKDAY, h19=RICH, h20=RICH), _morning(CHEAP_MORNING))

    assert len(proposal.slots) == 1
    slot = proposal.slots[0]
    assert (slot.start, slot.end) == (time(19, 0), time(21, 0))
    assert slot.target_soc == PARTIAL_TARGET_SOC


def test_three_expensive_hours_split_into_early_and_late():
    proposal = _propose(
        _day(WORKDAY, h19=RICH, h20=RICH, h21=RICH), _morning(CHEAP_MORNING)
    )

    early, late = proposal.slots
    assert early.kind is SlotKind.DISCHARGE_EVENING_EARLY
    assert (early.start, early.end) == (time(19, 0), time(21, 0))
    assert early.target_soc == PARTIAL_TARGET_SOC
    assert late.kind is SlotKind.DISCHARGE_EVENING_LATE
    assert (late.start, late.end) == (time(21, 0), time(22, 0))
    # Runs to the end of T2 — nothing left to reserve for.
    assert late.target_soc == FULL_TARGET_SOC


# ─── strategy follows the expensive end of the window ───


def test_strategy_starts_at_once_when_the_first_hour_pays_more():
    proposal = _propose(_day(WORKDAY, h19=900.0, h20=RICH), _morning(CHEAP_MORNING))

    assert proposal.slots[0].behavior is SlotBehavior.IMMEDIATE


def test_strategy_waits_when_the_second_hour_pays_more():
    proposal = _propose(_day(WORKDAY, h19=RICH, h20=900.0), _morning(CHEAP_MORNING))

    assert proposal.slots[0].behavior is SlotBehavior.DELAYED_TO_END


# ─── the morning filter ───


def test_a_better_morning_cancels_the_evening_entirely():
    proposal = _propose(_day(WORKDAY, h19=RICH, h20=RICH), _morning(RICH + 100))

    assert proposal.is_empty
    assert "tomorrow morning" in proposal.reason


def test_only_hours_beating_the_morning_survive():
    # 19 beats the morning, 20 does not — the window shrinks to one hour.
    proposal = _propose(_day(WORKDAY, h19=900.0, h20=RICH), _morning(RICH + 50))

    assert len(proposal.slots) == 1
    assert (proposal.slots[0].start, proposal.slots[0].end) == (
        time(19, 0),
        time(20, 0),
    )


def test_an_unpublished_tomorrow_leaves_the_evening_on_its_own_merits():
    proposal = _propose(_day(WORKDAY, h19=RICH, h20=RICH), None)

    assert not proposal.is_empty


# ─── threshold ───


def test_nothing_is_proposed_when_no_hour_clears_the_threshold():
    proposal = _propose(_day(WORKDAY, h19=POOR, h20=POOR), _morning(CHEAP_MORNING))

    assert proposal.is_empty
    assert f"{EXPORT_THRESHOLD:.0f}" in proposal.reason


def test_the_threshold_is_applied_on_gross_prices():
    just_under = (EXPORT_THRESHOLD / 1.23) - 1
    just_over = (EXPORT_THRESHOLD / 1.23) + 1

    assert _propose(_day(WORKDAY, h19=just_under), _morning(0)).is_empty
    assert not _propose(_day(WORKDAY, h19=just_over), _morning(0)).is_empty


# ─── zones and seasons ───


def test_a_gap_in_the_middle_produces_two_windows():
    proposal = _propose(
        _day(WORKDAY, h19=RICH, h20=POOR, h21=RICH), _morning(CHEAP_MORNING)
    )

    early, late = proposal.slots
    assert (early.start, early.end) == (time(19, 0), time(20, 0))
    assert (late.start, late.end) == (time(21, 0), time(22, 0))


def test_winter_shifts_the_window_three_hours_earlier():
    proposal = _propose(
        _day(WINTER_WORKDAY, h16=RICH, h17=RICH),
        _morning(CHEAP_MORNING),
        day=WINTER_WORKDAY,
    )

    slot = proposal.slots[0]
    assert (slot.start, slot.end) == (time(16, 0), time(18, 0))


def test_evening_hours_outside_the_expensive_zone_are_ignored_on_workdays():
    # 18:00 is T3 on a summer workday, however rich it looks.
    proposal = _propose(_day(WORKDAY, h18=900.0), _morning(CHEAP_MORNING))

    assert proposal.is_empty


def test_a_weekend_considers_the_whole_evening_and_always_empties():
    proposal = _propose(
        _day(WEEKEND, h17=RICH, h18=RICH),
        _morning(CHEAP_MORNING),
        day=WEEKEND,
        is_workday=False,
    )

    slot = proposal.slots[0]
    assert (slot.start, slot.end) == (time(17, 0), time(19, 0))
    # No expensive zone to reserve for.
    assert slot.target_soc == FULL_TARGET_SOC


def test_at_most_two_windows_are_proposed():
    proposal = _propose(
        _day(WEEKEND, h16=RICH, h18=RICH, h20=RICH, h22=RICH),
        _morning(CHEAP_MORNING),
        day=WEEKEND,
        is_workday=False,
    )

    assert len(proposal.slots) == 2
