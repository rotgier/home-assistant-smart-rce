"""Evening discharge proposal — the rules the user used to apply by hand.

Cases are named after the situations that actually occur: a single expensive
hour, a pair, a pair followed by a third, a dip in between, and the evening
that loses to the next morning. Prices in these tests are NET (as RCE
publishes them); the proposer converts to gross before comparing.
"""

from datetime import date, datetime, time

from custom_components.smart_rce.domain.battery_schedule import SlotBehavior, SlotKind
from custom_components.smart_rce.domain.evening_plan import EveningPlan, EveningWindow
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


EXPORT_THRESHOLD = EveningPlan.EXPORT_THRESHOLD
FULL_TARGET_SOC = EveningWindow.FULL_TARGET_SOC
PARTIAL_TARGET_SOC = EveningWindow.PARTIAL_TARGET_SOC


def _propose(today, tomorrow=None, *, day=WORKDAY, is_workday=True):
    return EveningPlan.for_day(day, today, is_workday=is_workday, tomorrow=tomorrow)


# ─── the three shapes the user described ───


def test_one_expensive_hour_gives_one_window_that_empties_what_it_can():
    proposal = _propose(_day(WORKDAY, h19=RICH), _morning(CHEAP_MORNING))

    assert len(proposal.windows) == 1
    slot = proposal.windows[0]
    assert (slot.start, slot.end) == (time(19, 0), time(20, 0))
    # A single hour cannot reach the target from a full battery anyway.
    assert slot.target_soc == FULL_TARGET_SOC


def test_two_expensive_hours_hold_back_for_the_remaining_t2_hour():
    # 19 and 20 qualify, 21 does not — but 21 is still inside T2, so the house
    # must be able to run off the battery rather than buy at the T2 rate.
    proposal = _propose(_day(WORKDAY, h19=RICH, h20=RICH), _morning(CHEAP_MORNING))

    assert len(proposal.windows) == 1
    slot = proposal.windows[0]
    assert (slot.start, slot.end) == (time(19, 0), time(21, 0))
    assert slot.target_soc == PARTIAL_TARGET_SOC


def test_three_expensive_hours_split_into_early_and_late():
    proposal = _propose(
        _day(WORKDAY, h19=RICH, h20=RICH, h21=RICH), _morning(CHEAP_MORNING)
    )

    early, late = proposal.windows
    assert (early.start, early.end) == (time(19, 0), time(21, 0))
    assert early.target_soc == PARTIAL_TARGET_SOC
    assert (late.start, late.end) == (time(21, 0), time(22, 0))
    # Runs to the end of T2 — nothing left to reserve for.
    assert late.target_soc == FULL_TARGET_SOC


# ─── strategy follows the expensive end of the window ───


def test_strategy_starts_at_once_when_the_first_hour_pays_more():
    proposal = _propose(_day(WORKDAY, h19=900.0, h20=RICH), _morning(CHEAP_MORNING))

    assert proposal.windows[0].behavior is SlotBehavior.IMMEDIATE


def test_strategy_waits_when_the_second_hour_pays_more():
    proposal = _propose(_day(WORKDAY, h19=RICH, h20=900.0), _morning(CHEAP_MORNING))

    assert proposal.windows[0].behavior is SlotBehavior.DELAYED_TO_END


# ─── the morning filter ───


def test_a_better_morning_cancels_the_evening_entirely():
    proposal = _propose(_day(WORKDAY, h19=RICH, h20=RICH), _morning(RICH + 100))

    assert proposal.is_empty
    assert "tomorrow morning" in proposal.reason


def test_only_hours_beating_the_morning_survive():
    # 19 beats the morning, 20 does not — the window shrinks to one hour.
    proposal = _propose(_day(WORKDAY, h19=900.0, h20=RICH), _morning(RICH + 50))

    assert len(proposal.windows) == 1
    assert (proposal.windows[0].start, proposal.windows[0].end) == (
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

    early, late = proposal.windows
    assert (early.start, early.end) == (time(19, 0), time(20, 0))
    assert (late.start, late.end) == (time(21, 0), time(22, 0))


def test_winter_considers_the_last_three_hours_of_its_longer_peak():
    # Winter T2 runs 16-21, but the battery empties in under two hours, so
    # only 18, 19 and 20 are of any use — starting at 16:00 would spend the
    # charge before the expensive evening peaks.
    proposal = _propose(
        _day(WINTER_WORKDAY, h18=RICH, h19=RICH),
        _morning(CHEAP_MORNING),
        day=WINTER_WORKDAY,
    )

    slot = proposal.windows[0]
    assert (slot.start, slot.end) == (time(18, 0), time(20, 0))


def test_winter_ignores_the_first_hours_of_its_peak():
    proposal = _propose(
        _day(WINTER_WORKDAY, h16=_HUGE, h17=_HUGE),
        _morning(CHEAP_MORNING),
        day=WINTER_WORKDAY,
    )

    assert proposal.is_empty


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

    slot = proposal.windows[0]
    assert (slot.start, slot.end) == (time(17, 0), time(19, 0))
    # No expensive zone to reserve for.
    assert slot.target_soc == FULL_TARGET_SOC


def test_a_day_off_takes_the_best_adjacent_pair():
    # 20 and 21 together beat any other neighbouring pair, so that is the
    # window — one slot, nothing held back, LATE unused.
    proposal = _propose(
        _day(WEEKEND, h17=RICH, h18=RICH, h20=_HUGE, h21=_MID),
        _morning(CHEAP_MORNING),
        day=WEEKEND,
        is_workday=False,
    )

    assert len(proposal.windows) == 1
    window = proposal.windows[0]
    assert (window.start, window.end) == (time(20, 0), time(22, 0))
    assert window.target_soc == FULL_TARGET_SOC


def test_a_day_off_never_fills_the_late_slot():
    proposal = _propose(
        _day(WEEKEND, h17=RICH, h18=RICH, h19=RICH, h20=RICH),
        _morning(CHEAP_MORNING),
        day=WEEKEND,
        is_workday=False,
    )

    late = [
        c for c in proposal.slot_commands() if c.kind is SlotKind.DISCHARGE_EVENING_LATE
    ]
    assert len(proposal.windows) == 1
    assert [c.value for c in late] == [False]


def test_a_day_off_falls_back_to_one_hour_when_none_adjoin():
    proposal = _propose(
        _day(WEEKEND, h17=RICH, h20=_HUGE),
        _morning(CHEAP_MORNING),
        day=WEEKEND,
        is_workday=False,
    )

    assert len(proposal.windows) == 1
    assert proposal.windows[0].start == time(20, 0)


# ─── the plan speaks the aggregate's language ───


def test_windows_become_commands_for_the_two_evening_slots():
    plan = _propose(
        _day(WORKDAY, h19=RICH, h20=RICH, h21=RICH), _morning(CHEAP_MORNING)
    )

    targets = {(c.kind, type(c).__name__) for c in plan.slot_commands()}
    assert (SlotKind.DISCHARGE_EVENING_EARLY, "SetSlotStartCommand") in targets
    assert (SlotKind.DISCHARGE_EVENING_LATE, "SetSlotStartCommand") in targets


def test_a_slot_without_a_window_is_switched_off():
    # One window only — LATE must be disabled, not left on yesterday's setting.
    plan = _propose(_day(WORKDAY, h19=RICH, h20=RICH), _morning(CHEAP_MORNING))

    late = [
        c for c in plan.slot_commands() if c.kind is SlotKind.DISCHARGE_EVENING_LATE
    ]
    assert len(late) == 1
    assert late[0].value is False


def test_an_empty_plan_switches_every_slot_off():
    plan = _propose(_day(WORKDAY, h19=POOR), _morning(CHEAP_MORNING))

    assert plan.is_empty
    assert [c.value for c in plan.slot_commands()] == [False, False, False]


def test_commands_carry_the_window_settings():
    plan = _propose(_day(WORKDAY, h19=RICH, h20=RICH), _morning(CHEAP_MORNING))

    values = {type(c).__name__: c.value for c in plan.slot_commands()}
    assert values["SetSlotStartCommand"] == time(19, 0)
    assert values["SetSlotEndCommand"] == time(21, 0)
    assert values["SetSlotTargetSocCommand"] == PARTIAL_TARGET_SOC


# ─── who may overwrite a hand-set schedule ───


def test_a_plan_emptied_by_the_morning_says_so():
    # The later run uses this to justify overwriting manual edits.
    plan = _propose(_day(WORKDAY, h19=RICH, h20=RICH), _morning(RICH + 100))

    assert plan.is_empty
    assert plan.overruled_by_morning


def test_an_ordinarily_empty_plan_is_not_grounds_to_overwrite():
    plan = _propose(_day(WORKDAY, h19=POOR), _morning(CHEAP_MORNING))

    assert plan.is_empty
    assert not plan.overruled_by_morning


def test_a_plan_left_intact_by_the_morning_claims_nothing():
    plan = _propose(_day(WORKDAY, h19=RICH, h20=RICH), _morning(CHEAP_MORNING))

    assert not plan.overruled_by_morning


# ─── window as a value object ───


def test_a_window_describes_itself_for_the_summary():
    plan = _propose(_day(WORKDAY, h19=RICH, h20=900.0), _morning(CHEAP_MORNING))

    assert plan.windows[0].describe() == "19:00-21:00 to 33% (delayed_to_end)"


def test_a_window_knows_whether_it_runs_to_a_given_hour():
    window = EveningWindow.covering(
        (19, 20), _day(WORKDAY, h19=RICH, h20=RICH), zone_end=22, is_last=True
    )

    assert window.reaches(21)
    assert not window.reaches(22)
    assert not window.is_single_hour


# ─── which evening a run targets ───


def test_a_summer_workday_switches_over_when_its_peak_ends_at_ten():
    assert not EveningPlan.plans_tomorrow_at(datetime(2026, 9, 21, 21, 59))
    assert EveningPlan.plans_tomorrow_at(datetime(2026, 9, 21, 22, 5))


def test_a_winter_workday_switches_over_an_hour_earlier():
    # Winter T2 ends at 21:00, so by 21:05 tonight is done and the run looks
    # ahead — a fixed 22:00 would have wasted that hour every winter evening.
    assert not EveningPlan.plans_tomorrow_at(datetime(2026, 10, 5, 20, 5))
    assert EveningPlan.plans_tomorrow_at(datetime(2026, 10, 5, 21, 5))


def test_a_day_off_switches_over_latest_of_all():
    # Its window runs to 23:00, so 22:05 still belongs to tonight.
    assert not EveningPlan.plans_tomorrow_at(
        datetime(2026, 9, 26, 22, 5), is_workday=False
    )
    assert EveningPlan.plans_tomorrow_at(datetime(2026, 9, 26, 23, 5), is_workday=False)


def test_after_midnight_a_run_is_back_to_planning_tonight():
    # The 00:05 retry sits inside the evening it is planning for.
    assert not EveningPlan.plans_tomorrow_at(datetime(2026, 9, 21, 0, 5))


# ─── a day off where the morning wins outright ───


def test_a_day_off_with_nothing_left_after_the_morning_filter_is_empty():
    """Every evening hour vetoed on a day off used to raise ValueError.

    Real case, 04.10.2026: the evening paid 702-1255 gross, which clears the
    export threshold, but Monday 07:00 paid 1309 and outbid all of it. The
    workday path reaches this state through `_contiguous_runs`, which returns
    no runs; the day-off path went to `_best_off_peak_pair`, whose fallback
    called `max()` on an empty list. The afternoon sweep died every five
    minutes until the Telegram guard said so.
    """
    plan = _propose(
        _day(WEEKEND, h17=1003.0, h18=1173.0, h19=1255.0, h20=1195.0),
        _morning(1309.0),
        day=WEEKEND,
        is_workday=False,
    )

    assert plan.is_empty
    assert plan.overruled_by_morning
    assert [c.value for c in plan.slot_commands()] == [False, False, False]


def test_a_day_off_with_nothing_qualifying_at_all_is_empty_too():
    """Same shape without a morning to blame — simply no hour worth selling."""
    plan = _propose(_day(WEEKEND), day=WEEKEND, is_workday=False)

    assert plan.is_empty
    assert not plan.overruled_by_morning


# ─── the three-rung ladder ───

# A trio that falls from hour to hour. Gross: ~2950 / ~2090 / ~1600.
_LADDER_DAY = {"h19": 2400.0, "h20": 1700.0, "h21": 1300.0}


def test_a_falling_trio_becomes_three_one_hour_rungs():
    plan = _propose(_day(WORKDAY, **_LADDER_DAY), _morning(CHEAP_MORNING))

    assert [(w.start, w.end) for w in plan.windows] == [
        (time(19, 0), time(20, 0)),
        (time(20, 0), time(21, 0)),
        (time(21, 0), time(22, 0)),
    ]
    assert [w.target_soc for w in plan.windows] == [47.0, 33.0, 10.0]
    assert [w.behavior for w in plan.windows] == [
        SlotBehavior.DELAYED_TO_END,
        SlotBehavior.DELAYED_TO_END,
        SlotBehavior.IMMEDIATE,
    ]


def test_the_two_upper_rungs_wait_so_they_land_on_target_as_the_hour_turns():
    """The whole point of the ladder: be standing on the reserve at the boundary.

    IMMEDIATE would reach the target early and hand the rest of the hour to
    the house, which quietly eats into the reserve the next hour needs.
    """
    plan = _propose(_day(WORKDAY, **_LADDER_DAY), _morning(CHEAP_MORNING))

    assert all(w.behavior is SlotBehavior.DELAYED_TO_END for w in plan.windows[:2])


def test_the_ladder_fills_all_three_slots_in_order():
    plan = _propose(_day(WORKDAY, **_LADDER_DAY), _morning(CHEAP_MORNING))

    enabled = [
        c.kind.name
        for c in plan.slot_commands()
        if type(c).__name__ == "SetSlotEnabledCommand" and c.value
    ]
    assert enabled == [
        "DISCHARGE_EVENING_EARLY",
        "DISCHARGE_EVENING_MID",
        "DISCHARGE_EVENING_LATE",
    ]


def test_a_peak_in_the_middle_is_left_to_the_price_driven_path():
    """Not a falling trio — and that path already lands on the reserve itself."""
    plan = _propose(
        _day(WORKDAY, h19=1300.0, h20=2400.0, h21=1700.0), _morning(CHEAP_MORNING)
    )

    assert len(plan.windows) == 2
    assert plan.windows[0].behavior is SlotBehavior.DELAYED_TO_END


def test_a_rising_trio_is_left_alone():
    plan = _propose(
        _day(WORKDAY, h19=1300.0, h20=1700.0, h21=2400.0), _morning(CHEAP_MORNING)
    )

    assert len(plan.windows) == 2


def test_equal_hours_are_not_a_falling_trio():
    """`>` not `>=`.

    What follows is then the price-driven path's business — here it sells the
    reserve early, because 20:00 beats 21:00 by more than the zone gap. The
    point of the assertion is only that the ladder stood down.
    """
    plan = _propose(
        _day(WORKDAY, h19=2400.0, h20=2400.0, h21=1300.0), _morning(CHEAP_MORNING)
    )

    assert len(plan.windows) < 3
    assert all(w.target_soc != 47.0 for w in plan.windows)


def test_only_two_qualifying_hours_keep_the_old_pair():
    """The ladder needs three hours; two stay on EARLY + LATE, MID unused."""
    plan = _propose(_day(WORKDAY, h19=2400.0, h20=1700.0), _morning(CHEAP_MORNING))

    enabled = [
        c.kind.name
        for c in plan.slot_commands()
        if type(c).__name__ == "SetSlotEnabledCommand" and c.value
    ]
    assert "DISCHARGE_EVENING_MID" not in enabled


def test_a_day_off_never_climbs_the_ladder():
    """Days off have no T2 to ration — one window, and MID stays out of it."""
    plan = _propose(
        _day(WEEKEND, h17=2400.0, h18=1700.0, h19=1300.0),
        _morning(CHEAP_MORNING),
        day=WEEKEND,
        is_workday=False,
    )

    assert len(plan.windows) == 1


# ─── selling the reserve early instead of holding it ───

# Gross zone gap is T2 minus T3 (~870 at the 2026 tariff), and the margin
# demands a quarter more. Net prices below are chosen either side of that.
_HUGE = 2400.0  # ~2950 gross
_MID = 1700.0  # ~2090 gross
_SMALL = 640.0  # ~790 gross


def test_a_wide_gap_before_the_last_hour_keeps_one_window():
    # 19 and 20 pay hugely, 21 barely clears the threshold: selling the
    # reserve into the peak beats saving it for the T2 hour.
    #
    # The peak sits at 20:00, not 19:00. A trio that falls all the way belongs
    # to the ladder now, so the early-sale rule is only ever asked about
    # evenings shaped like this one.
    proposal = _propose(
        _day(WORKDAY, h19=_MID, h20=_HUGE, h21=_SMALL), _morning(CHEAP_MORNING)
    )

    assert len(proposal.windows) == 1
    window = proposal.windows[0]
    assert (window.start, window.end) == (time(19, 0), time(22, 0))
    assert window.target_soc == FULL_TARGET_SOC


def test_a_narrow_gap_still_splits_into_two_windows():
    # All three hours are comparable — holding a reserve for 21:00 wins.
    proposal = _propose(
        _day(WORKDAY, h19=RICH, h20=RICH, h21=RICH), _morning(CHEAP_MORNING)
    )

    assert len(proposal.windows) == 2


def test_the_margin_is_what_rejects_a_gap_that_merely_clears_break_even():
    # Gap just over the raw zone difference but under it times the margin.
    from custom_components.smart_rce.domain.evening_plan import EveningPlan
    from custom_components.smart_rce.tariff import Zone
    from custom_components.smart_rce.tariff.table import latest_rates

    rates = latest_rates()
    zone_gap = rates.marginal_cost(Zone.T2) - rates.marginal_cost(Zone.T3)
    last = 900.0  # net
    bare = last + (zone_gap * 1.05) / 1.23  # clears break-even, not the margin
    ample = last + (zone_gap * EveningPlan._SELL_EARLY_MARGIN * 1.2) / 1.23

    # 19:00 matches 20:00 rather than beating it, so the trio does not fall
    # all the way and the ladder leaves the decision here.
    assert (
        len(_propose(_day(WORKDAY, h19=bare, h20=bare, h21=last), _morning(0)).windows)
        == 2
    )
    assert (
        len(
            _propose(_day(WORKDAY, h19=ample, h20=ample, h21=last), _morning(0)).windows
        )
        == 1
    )


def test_a_day_off_never_asks_the_question():
    # No T2 to reserve against, and only ever one window — so the early-sale
    # rule has nothing to decide there.
    proposal = _propose(
        _day(WEEKEND, h17=_HUGE, h18=_MID, h19=_SMALL),
        _morning(CHEAP_MORNING),
        day=WEEKEND,
        is_workday=False,
    )

    assert len(proposal.windows) == 1
