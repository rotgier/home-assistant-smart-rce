"""Evening discharge plan — the rules the user used to apply by hand.

Workdays (user's decision 23.10.2026): every T2 hour sells on its own when it
clears the bar, beats tomorrow morning and no later evening hour pays more; a
selling hour stands on the reserve the T2 hours after it need. Days off: the
best pair of hours from 16:00, emptied in one window. Prices in these tests
are NET (as RCE publishes them) unless built with `_gross`; the plan compares
gross.
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
_HUGE = 2400.0  # ~2950 gross
_MID = 1700.0  # ~2090 gross
_SMALL = 640.0  # ~790 gross

EXPORT_THRESHOLD = EveningPlan.EXPORT_THRESHOLD
FULL_TARGET_SOC = EveningWindow.FULL_TARGET_SOC

XXEARLY = SlotKind.DISCHARGE_EVENING_XXEARLY
XEARLY = SlotKind.DISCHARGE_EVENING_XEARLY
EARLY = SlotKind.DISCHARGE_EVENING_EARLY
MID = SlotKind.DISCHARGE_EVENING_MID
LATE = SlotKind.DISCHARGE_EVENING_LATE
DELAYED = SlotBehavior.DELAYED_TO_END
IMMEDIATE = SlotBehavior.IMMEDIATE


def _day(day: date, **hours: float) -> RceDayPrices:
    prices = [POOR] * 24
    for hour, price in hours.items():
        prices[int(hour[1:])] = price
    return RceDayPrices(published_at=None, day=day, hour_price=tuple(prices))


def _gross(day: date, by_hour: dict[int, float]) -> RceDayPrices:
    """Build a day from gross prices (as the dashboard shows them), the rest POOR."""
    prices = [POOR] * 24
    for hour, gross in by_hour.items():
        prices[hour] = gross / 1.23
    return RceDayPrices(published_at=None, day=day, hour_price=tuple(prices))


def _morning(price: float) -> RceDayPrices:
    prices = [CHEAP_MORNING] * 24
    for hour in range(5, 9):
        prices[hour] = price
    return RceDayPrices(
        published_at=None, day=date(2026, 9, 22), hour_price=tuple(prices)
    )


def _morning_gross(price: float) -> RceDayPrices:
    return _morning(price / 1.23)


def _propose(today, tomorrow=None, *, day=WORKDAY, is_workday=True):
    return EveningPlan.for_day(day, today, is_workday=is_workday, tomorrow=tomorrow)


def _rungs(plan: EveningPlan) -> list[tuple[int, float, SlotKind]]:
    """(hour, target, slot) of every window, read back from the commands."""
    by_kind: dict[SlotKind, dict[str, object]] = {}
    for c in plan.slot_commands():
        by_kind.setdefault(c.kind, {})[type(c).__name__] = c.value
    return [
        (cmds["SetSlotStartCommand"].hour, cmds["SetSlotTargetSocCommand"], kind)  # type: ignore[union-attr]
        for kind, cmds in by_kind.items()
        if cmds.get("SetSlotEnabledCommand")
    ]


# ─── one rung per selling hour, the reserve by the T2 hours left ───


def test_a_falling_winter_evening_sells_in_all_five_hours():
    plan = _propose(
        _gross(WINTER_WORKDAY, {16: 2000, 17: 1900, 18: 1800, 19: 1700, 20: 1600}),
        _morning(CHEAP_MORNING),
        day=WINTER_WORKDAY,
    )

    assert _rungs(plan) == [
        (16, 75.0, XXEARLY),
        (17, 61.0, XEARLY),
        (18, 47.0, EARLY),
        (19, 33.0, MID),
        (20, 10.0, LATE),
    ]
    # Every rung but the last stands on its target as the hour closes.
    assert [w.behavior for w in plan.windows] == [DELAYED] * 4 + [IMMEDIATE]
    assert all((w.end.hour - w.start.hour) == 1 for w in plan.windows)


def test_an_hour_holds_when_a_later_one_pays_more():
    # 16:00 beats 17-19 but 20:00 pays more than 17, 18 and 19: those three
    # hold for it. The slot follows the hour, not the count of windows.
    plan = _propose(
        _gross(WINTER_WORKDAY, {16: 1200, 17: 800, 18: 850, 19: 900, 20: 1000}),
        _morning(CHEAP_MORNING),
        day=WINTER_WORKDAY,
    )

    assert _rungs(plan) == [(16, 75.0, XXEARLY), (20, 10.0, LATE)]


def test_a_tie_sells_in_the_earlier_hour():
    plan = _propose(
        _gross(WINTER_WORKDAY, {18: 1000, 19: 1000}),
        _morning(CHEAP_MORNING),
        day=WINTER_WORKDAY,
    )

    assert _rungs(plan) == [(18, 47.0, EARLY), (19, 33.0, MID)]


def test_the_last_hour_holds_for_a_dearer_hour_after_t2_21_10_2026():
    # RCE net 21.10.2026: 18-20 fall (907, 835, 759 gross), 21:00 pays 819.
    # 16 and 17 hold for the dearer 18:00.
    prices = RceDayPrices(
        published_at=None,
        day=date(2026, 10, 21),
        hour_price=(
            606.87,
            606.01,
            594.91,
            592.57,
            610.93,
            695.46,
            770.57,
            840.29,
            710.37,
            623.04,
            645.35,
            598.11,
            589.57,
            594.94,
            701.87,
            731.86,
            641.77,
            697.57,
            737.19,
            678.59,
            616.97,
            666.01,
            665.96,
            602.81,
        ),  # fmt: skip
    )
    plan = EveningPlan.for_day(date(2026, 10, 21), prices, is_workday=True)

    assert _rungs(plan) == [(18, 47.0, EARLY), (19, 33.0, MID)]


def test_the_morning_takes_the_last_hour_1_10_2026():
    # Gross 1.10.2026; 2.10 morning paid 1141, more than 20:00 (927). The plan
    # of the day sold 18-20 to 33% at once and ran out of hour at 19:20.
    plan = _propose(
        _gross(
            date(2026, 10, 1), {16: 864, 17: 942, 18: 1153, 19: 1150, 20: 927, 21: 882}
        ),
        _morning_gross(1141),
        day=date(2026, 10, 1),
    )

    assert _rungs(plan) == [(18, 47.0, EARLY), (19, 33.0, MID)]


def test_the_peak_at_17_takes_the_new_slot_20_10_2026():
    # Gross 20.10.2026, 21.10 morning 1034: 16:00 holds for 17:00, 19:00 and
    # 20:00 lose to the morning.
    plan = _propose(
        _gross(
            date(2026, 10, 20),
            {16: 1100, 17: 1152, 18: 1109, 19: 982, 20: 857, 21: 763},
        ),
        _morning_gross(1034),
        day=date(2026, 10, 20),
    )

    assert _rungs(plan) == [(17, 61.0, XEARLY), (18, 47.0, EARLY)]


def test_23_10_2026_sells_from_the_peak_and_keeps_the_last_reserve():
    # The plan of the day sold 18-20 to 33% at once (reached 18:40) and the
    # house barely made it to 21:00. 24.10 morning paid 837 > 20:00 (829).
    plan = _propose(
        _gross(
            date(2026, 10, 23),
            {16: 1060, 17: 1435, 18: 1363, 19: 1084, 20: 829, 21: 689},
        ),
        _morning_gross(837),
        day=date(2026, 10, 23),
    )

    assert _rungs(plan) == [(17, 61.0, XEARLY), (18, 47.0, EARLY), (19, 33.0, MID)]
    assert all(w.behavior is DELAYED for w in plan.windows)


def test_a_summer_evening_uses_the_three_slots_it_always_had():
    plan = _propose(
        _day(WORKDAY, h19=_HUGE, h20=_MID, h21=_SMALL), _morning(CHEAP_MORNING)
    )

    assert _rungs(plan) == [(19, 47.0, EARLY), (20, 33.0, MID), (21, 10.0, LATE)]
    disabled = {
        c.kind
        for c in plan.slot_commands()
        if type(c).__name__ == "SetSlotEnabledCommand" and not c.value
    }
    assert disabled == {XXEARLY, XEARLY}


def test_a_lone_hour_in_the_middle_of_t2_holds_its_reserve():
    # 19:00 alone with 20:00 and 21:00 still summer T2: one rung, not empty.
    plan = _propose(_day(WORKDAY, h19=RICH), _morning(CHEAP_MORNING))

    assert _rungs(plan) == [(19, 47.0, EARLY)]


def test_a_cheap_hour_in_the_middle_leaves_a_gap():
    plan = _propose(
        _day(WORKDAY, h19=RICH, h20=POOR, h21=RICH), _morning(CHEAP_MORNING)
    )

    assert _rungs(plan) == [(19, 47.0, EARLY), (21, 10.0, LATE)]


def test_hours_outside_the_expensive_zone_are_ignored_on_workdays():
    # 18:00 is T3 on a summer workday, however rich it looks.
    assert _propose(_day(WORKDAY, h18=900.0), _morning(CHEAP_MORNING)).is_empty


# ─── the morning filter ───


def test_a_better_morning_cancels_the_evening_entirely():
    proposal = _propose(_day(WORKDAY, h19=RICH, h20=RICH), _morning(RICH + 100))

    assert proposal.is_empty
    assert "tomorrow morning" in proposal.reason


def test_only_hours_beating_the_morning_survive():
    # 19 beats the morning, 20 does not.
    proposal = _propose(_day(WORKDAY, h19=900.0, h20=RICH), _morning(RICH + 50))

    assert _rungs(proposal) == [(19, 47.0, EARLY)]


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


# ─── days off ───


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
    # window — one slot, nothing held back.
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


def test_a_day_off_uses_the_early_slot_alone():
    proposal = _propose(
        _day(WEEKEND, h17=RICH, h18=RICH, h19=RICH, h20=RICH),
        _morning(CHEAP_MORNING),
        day=WEEKEND,
        is_workday=False,
    )

    assert [kind for _h, _t, kind in _rungs(proposal)] == [EARLY]


def test_a_day_off_falls_back_to_one_hour_when_none_adjoin():
    proposal = _propose(
        _day(WEEKEND, h17=RICH, h20=_HUGE),
        _morning(CHEAP_MORNING),
        day=WEEKEND,
        is_workday=False,
    )

    assert len(proposal.windows) == 1
    assert proposal.windows[0].start == time(20, 0)


def test_a_day_off_window_pushes_toward_its_dearer_end():
    first = _propose(
        _day(WEEKEND, h19=900.0, h20=RICH), day=WEEKEND, is_workday=False
    ).windows[0]
    second = _propose(
        _day(WEEKEND, h19=RICH, h20=900.0), day=WEEKEND, is_workday=False
    ).windows[0]

    assert first.behavior is IMMEDIATE
    assert second.behavior is DELAYED


def test_a_day_off_with_nothing_left_after_the_morning_filter_is_empty():
    """Every evening hour vetoed on a day off used to raise ValueError.

    Real case, 04.10.2026: the evening paid 702-1255 gross, which clears the
    export threshold, but Monday 07:00 paid 1309 and outbid all of it; the
    fallback called `max()` on an empty list.
    """
    plan = _propose(
        _day(WEEKEND, h17=1003.0, h18=1173.0, h19=1255.0, h20=1195.0),
        _morning(1309.0),
        day=WEEKEND,
        is_workday=False,
    )

    assert plan.is_empty
    assert plan.overruled_by_morning
    assert [c.value for c in plan.slot_commands()] == [False] * 5


def test_a_day_off_with_nothing_qualifying_at_all_is_empty_too():
    """Same shape without a morning to blame — simply no hour worth selling."""
    plan = _propose(_day(WEEKEND), day=WEEKEND, is_workday=False)

    assert plan.is_empty
    assert not plan.overruled_by_morning


# ─── the plan speaks the aggregate's language ───


def test_a_slot_without_a_window_is_switched_off():
    plan = _propose(_day(WORKDAY, h19=RICH, h20=RICH), _morning(CHEAP_MORNING))

    late = [c for c in plan.slot_commands() if c.kind is LATE]
    assert len(late) == 1
    assert late[0].value is False


def test_an_empty_plan_switches_every_slot_off():
    plan = _propose(_day(WORKDAY, h19=POOR), _morning(CHEAP_MORNING))

    assert plan.is_empty
    assert [c.value for c in plan.slot_commands()] == [False] * 5


def test_commands_carry_the_window_settings():
    plan = _propose(_day(WORKDAY, h19=RICH, h20=RICH), _morning(CHEAP_MORNING))

    early = {type(c).__name__: c.value for c in plan.slot_commands() if c.kind is EARLY}
    assert early == {
        "SetSlotEnabledCommand": True,
        "SetSlotStartCommand": time(19, 0),
        "SetSlotEndCommand": time(20, 0),
        "SetSlotTargetSocCommand": 47.0,
        "SetSlotBehaviorCommand": DELAYED,
    }


def test_a_window_describes_itself_for_the_summary():
    plan = _propose(_day(WORKDAY, h19=RICH, h20=900.0), _morning(CHEAP_MORNING))

    assert [w.describe() for w in plan.windows] == [
        "20:00-21:00 to 33% (delayed_to_end)"
    ]


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


# ─── which evening a run targets ───


def test_a_summer_workday_switches_over_when_its_peak_ends_at_ten():
    assert not EveningPlan.plans_tomorrow_at(datetime(2026, 9, 21, 21, 59))
    assert EveningPlan.plans_tomorrow_at(datetime(2026, 9, 21, 22, 5))


def test_a_winter_workday_switches_over_an_hour_earlier():
    # Winter T2 ends at 21:00, so by 21:05 tonight is done and the run looks
    # ahead — a fixed 22:00 would have wasted that hour every winter evening.
    assert not EveningPlan.plans_tomorrow_at(datetime(2026, 10, 5, 20, 5))
    assert EveningPlan.plans_tomorrow_at(datetime(2026, 10, 5, 21, 5))


def test_with_tonight_done_a_day_off_switches_over_with_the_season():
    # A day off has no T2, but the switchover follows the season's T2 end
    # (17.10.2026): winter 21:00, summer 22:00, unless a slot runs later.
    assert EveningPlan.plans_tomorrow_at(datetime(2026, 10, 17, 21, 5))
    assert not EveningPlan.plans_tomorrow_at(datetime(2026, 9, 26, 21, 5))
    assert EveningPlan.plans_tomorrow_at(datetime(2026, 9, 26, 22, 5))


def test_a_slot_still_set_for_tonight_holds_the_switchover_back():
    late = time(23, 0)
    assert not EveningPlan.plans_tomorrow_at(
        datetime(2026, 10, 17, 22, 59), slots_end=late
    )
    assert EveningPlan.plans_tomorrow_at(datetime(2026, 10, 17, 23, 0), slots_end=late)


def test_a_slot_ending_mid_hour_holds_it_to_the_next_full_hour():
    assert not EveningPlan.plans_tomorrow_at(
        datetime(2026, 10, 17, 21, 45), slots_end=time(21, 30)
    )
    assert EveningPlan.plans_tomorrow_at(
        datetime(2026, 10, 17, 22, 0), slots_end=time(21, 30)
    )


def test_a_slot_ending_before_the_season_hour_changes_nothing():
    assert not EveningPlan.plans_tomorrow_at(
        datetime(2026, 10, 17, 20, 30), slots_end=time(20, 0)
    )
    assert EveningPlan.plans_tomorrow_at(
        datetime(2026, 10, 17, 21, 0), slots_end=time(20, 0)
    )


def test_after_midnight_a_run_is_back_to_planning_tonight():
    # The 00:05 retry sits inside the evening it is planning for.
    assert not EveningPlan.plans_tomorrow_at(datetime(2026, 9, 21, 0, 5))
