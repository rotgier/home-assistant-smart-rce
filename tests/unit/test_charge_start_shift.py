"""Charge-start shift — candidate rule, price window, confirmation and lock."""

from datetime import date
import json
from pathlib import Path

from custom_components.smart_rce.domain.charge_start_shift import (
    Candidate,
    ChargeStartShift,
    RewindWatch,
    candidate_start,
    cheaper_later_start,
    find_candidate,
    guarded_candidate,
    price_window_end,
)
from custom_components.smart_rce.domain.pv_surplus import (
    SurplusProfile,
    slots_from_hourly,
)
import pytest

DAY = date(2026, 10, 12)
H = 60

# Cheapest around noon: 11-14 cheapest, morning and evening dearer.
PRICES = [
    500, 500, 500, 500, 500, 500, 450, 420, 400, 380, 300, 200,
    100, 100, 200, 400, 600, 700, 700, 650, 600, 550, 500, 500,
]  # fmt: skip


def _profile(pv_by_hour: dict[int, float]) -> SurplusProfile:
    """Flat consumption (0.9 kWh/h), PV spread evenly over each hour."""
    return SurplusProfile.build(DAY, slots_from_hourly(pv_by_hour), None)


class TestPriceWindow:
    def test_ends_at_first_hour_pricier_than_the_one_before(self):
        # Start 12: hour 11 costs 200; 12, 13 cheaper, 14 = 200 not cheaper.
        assert price_window_end(12 * H, PRICES) == 14
        # Start 10: hour 9 costs 380 — 10..14 cheaper, 15 = 400 is not.
        assert price_window_end(10 * H, PRICES) == 15

    def test_half_hour_start_compares_with_its_own_hour(self):
        # 9:30 -> reference hour 9 (380), same window as a 10:00 start.
        assert price_window_end(9 * H + 30, PRICES) == 15


class TestCandidate:
    def test_no_sun_before_s0_keeps_s0(self):
        profile = _profile({12: 5.0, 13: 5.0})
        assert candidate_start(profile, PRICES, 7 * H + 41, 8.0, 12 * H) == 12 * H

    def test_enough_sun_in_the_s0_window_keeps_s0(self):
        profile = _profile(dict.fromkeys(range(8, 16), 4.0))
        # Window of 12:00 is 12-14 → 2 h × 3.1 = 6.2 kWh.
        assert candidate_start(profile, PRICES, 7 * H + 41, 6.0, 12 * H) == 12 * H

    def test_takes_the_latest_start_that_is_enough(self):
        profile = _profile(dict.fromkeys(range(8, 16), 4.0))
        # 12:00 gives 6.2, 11:00 (window 11-15) 12.4, 10:00 (10-15) 15.5.
        assert candidate_start(profile, PRICES, 7 * H + 41, 9.0, 12 * H) == 11 * H
        assert candidate_start(profile, PRICES, 7 * H + 41, 13.0, 12 * H) == 10 * H

    def test_never_before_eight_nor_before_the_sun(self):
        profile = _profile(dict.fromkeys(range(6, 16), 4.0))
        # 9:00 (window 9-15) gives 18.6, 8:00 (8-16) 24.8; sun from 6 changes nothing.
        assert candidate_start(profile, PRICES, 7 * H + 41, 20.0, 12 * H) == 8 * H
        late_sun = _profile(dict.fromkeys(range(10, 16), 4.0))
        # 10:00 (window 10-15) gives 15.5 — enough, but not before the sun.
        assert candidate_start(late_sun, PRICES, 7 * H + 41, 15.0, 12 * H) == 10 * H

    def test_short_even_at_the_earliest_start_opens_at_eight(self):
        # The sun cannot fill the battery: catch whatever there is from 08:00,
        # whether or not 8:00 itself has sun.
        late_sun = _profile(dict.fromkeys(range(10, 16), 4.0))
        assert find_candidate(late_sun, PRICES, 7 * H + 41, 50.0, 12 * H) == (
            Candidate(8 * H, False)
        )
        no_sun_before_s0 = _profile({12: 2.0, 13: 2.0})
        assert find_candidate(no_sun_before_s0, PRICES, 7 * H + 41, 8.0, 12 * H) == (
            Candidate(8 * H, False)
        )

    def test_short_with_s0_before_eight_keeps_s0(self):
        profile = _profile(dict.fromkeys(range(7, 16), 1.0))
        assert candidate_start(profile, PRICES, 6 * H, 50.0, 7 * H) == 7 * H

    def test_a_start_already_past_means_now(self):
        profile = _profile(dict.fromkeys(range(8, 16), 4.0))
        now = 9 * H + 11
        assert candidate_start(profile, PRICES, now, 50.0, 12 * H) == now

    def test_half_hour_s0_is_kept_and_whole_hours_follow(self):
        profile = _profile(dict.fromkeys(range(8, 16), 4.0))
        s0 = 9 * H + 30
        # 9:30 window 9:30-15:00 = 5.5 h × 1.55 × 2 = 17.05 kWh.
        assert candidate_start(profile, PRICES, 7 * H + 41, 17.0, s0) == s0
        # 9:00 (window 9-15, hour 8 = 400) adds the first half of hour 9.
        assert candidate_start(profile, PRICES, 7 * H + 41, 18.0, s0) == 9 * H


class TestCheaperLaterStart:
    """28.10.2026: S0 09:00 (990 net), 10:00 the cheapest hour (824)."""

    PRICES_28 = [
        773, 805, 782, 775, 782, 792, 1227, 1297, 1268, 990, 824, 964,
        1015, 1224, 1783, 2173, 2222, 2167, 1965, 1991, 1564, 1232, 1117, 963,
    ]  # fmt: skip
    SUNNY = dict.fromkeys(range(8, 15), 4.0)

    def _later(self, need, *, prices=None, base=None, pv=None):
        profile = _profile(pv or self.SUNNY)
        prices = prices or self.PRICES_28
        base = base or find_candidate(profile, prices, 8 * H + 41, need, 9 * H)
        return cheaper_later_start(
            profile, prices, 8 * H + 41, base, lambda _s: need, window_end=16 * H
        )

    def test_little_to_fill_waits_for_the_cheapest_hour(self):
        # Battery at ~90%: about 1 kWh, filled within 10:00.
        assert self._later(1.0) == Candidate(10 * H, True)

    def test_a_large_need_stays_at_the_base_start(self):
        # From ~20%: 10:00 onwards no longer keeps the 1.5x margin by 15:00.
        assert self._later(15.0).start == 9 * H

    def test_a_base_short_of_sun_is_left_alone(self):
        short = Candidate(8 * H, False)
        assert self._later(1.0, base=short) is short

    def test_near_equal_hours_do_not_move_the_start(self):
        # 10:00 only 5 zl/MWh cheaper than 9:00 — under LATER_MIN_GAIN.
        prices = list(self.PRICES_28)
        prices[10] = prices[9] - 5
        prices[11] = prices[12] = prices[13] = 2000
        assert self._later(1.0, prices=prices).start == 9 * H

    def test_not_full_an_hour_before_t2_is_not_a_start(self):
        # Sun only up to 11:00: from 10:00 1 h of sun, not 1.5x a 3 kWh need.
        pv = dict.fromkeys(range(8, 11), 4.0)
        assert self._later(3.0, pv=pv).start == 9 * H


class TestGuarded:
    """Variant A: earlier moves wait for 09:10 and Extrap, unless Live is short."""

    def test_flags_a_forecast_short_even_at_the_earliest_start(self):
        profile = _profile(dict.fromkeys(range(8, 16), 4.0))
        assert find_candidate(profile, PRICES, 7 * H + 41, 9.0, 12 * H) == (
            Candidate(11 * H, True)
        )
        assert find_candidate(profile, PRICES, 7 * H + 41, 50.0, 12 * H) == (
            Candidate(8 * H, False)
        )

    def test_before_0910_moves_earlier_only_when_even_the_earliest_is_short(self):
        now = 8 * H + 1
        assert guarded_candidate(Candidate(10 * H, True), None, 12 * H, now) == 12 * H
        assert guarded_candidate(Candidate(8 * H, False), None, 12 * H, now) == 8 * H

    def test_from_0910_live_and_extrap_must_agree(self):
        now = 9 * H + 11
        live = Candidate(10 * H, True)
        assert guarded_candidate(live, 11 * H, 12 * H, now) == 11 * H  # later of two
        assert guarded_candidate(live, 12 * H, 12 * H, now) == 12 * H  # Extrap: no
        assert guarded_candidate(live, None, 12 * H, now) == 12 * H  # no Extrap

    def test_later_moves_follow_live(self):
        assert guarded_candidate(Candidate(11 * H, True), None, 9 * H, 8 * H) == 11 * H


class TestRewindWatch:
    """Alert (not applied) when a later start would still fill the battery."""

    def test_alerts_after_three_agreeing_evaluations(self):
        watch = RewindWatch(DAY)
        live, extrap = Candidate(11 * H, True), Candidate(11 * H + 30, True)
        assert watch.evaluate(9 * H + 1, live, extrap) is None
        assert watch.evaluate(9 * H + 6, live, extrap) is None
        assert watch.evaluate(9 * H + 11, live, extrap) == 11 * H  # earlier of two
        assert watch.alerts == 1 and watch.recent == ()

    def test_needs_both_to_fill_the_battery(self):
        watch = RewindWatch(DAY)
        for i in range(3):
            short = Candidate(11 * H, False)
            assert watch.evaluate(9 * H + 5 * i, short, Candidate(11 * H, True)) is None
        for i in range(3):
            assert watch.evaluate(10 * H + 5 * i, Candidate(11 * H, True), None) is None
        assert watch.alerts == 0

    def test_needs_at_least_thirty_minutes(self):
        watch = RewindWatch(DAY)
        later = Candidate(9 * H + 30, True)
        for i in range(3):
            assert watch.evaluate(9 * H + 1 + 5 * i, later, later) is None

    def test_at_most_three_alerts_a_day(self):
        watch = RewindWatch(DAY)
        later = Candidate(12 * H, True)
        fired = [watch.evaluate(9 * H + 5 * i, later, later) for i in range(15)]
        assert sum(f is not None for f in fired) == 3


class TestConfirmation:
    def test_moves_earlier_after_three_by_the_least(self):
        shift = ChargeStartShift(day=DAY, s0=12 * H)
        assert not shift.evaluate(8 * H, 10 * H, None)
        assert not shift.evaluate(8 * H + 5, 9 * H, None)
        assert shift.evaluate(8 * H + 10, 11 * H, None)
        assert shift.start == 11 * H

    def test_mixed_sides_do_not_move(self):
        shift = ChargeStartShift(day=DAY, s0=12 * H, start=10 * H)
        for now, cand in ((8 * H, 9 * H), (8 * H + 5, 11 * H), (8 * H + 10, 9 * H)):
            assert not shift.evaluate(now, cand, None)
        assert shift.start == 10 * H

    def test_moves_later_by_the_least_of_three_past_s0_too(self):
        # Since 28.10.2026 a cheaper later start may lie past S0.
        shift = ChargeStartShift(day=DAY, s0=11 * H, start=9 * H)
        for i, cand in enumerate((12 * H, 11 * H + 30, 13 * H)):
            moved = shift.evaluate(8 * H + 5 * i, cand, None)
        assert moved and shift.start == 11 * H + 30

    def test_a_start_past_s0_survives_the_round_trip(self):
        shift = ChargeStartShift(day=DAY, s0=9 * H, start=10 * H)
        restored = ChargeStartShift.from_dict(shift.to_dict())
        assert restored is not None and restored.start == 10 * H

    def test_does_not_move_earlier_once_the_start_has_passed(self):
        shift = ChargeStartShift(day=DAY, s0=12 * H, start=9 * H)
        for i in range(3):
            assert not shift.evaluate(9 * H + 5 * i, 8 * H, None)
        assert shift.start == 9 * H


class TestLock:
    def test_locks_after_soc_rises_by_one_pp_since_the_start(self):
        shift = ChargeStartShift(day=DAY, s0=10 * H)
        shift.evaluate(9 * H + 56, 10 * H, 40.0)
        shift.evaluate(10 * H + 1, 10 * H, 40.0)  # baseline
        assert shift.soc_at_start == 40.0 and not shift.locked
        shift.evaluate(10 * H + 6, 10 * H, 40.0)
        assert not shift.locked
        shift.evaluate(10 * H + 11, 10 * H, 41.0)
        assert shift.locked
        # Locked: nothing moves it any more.
        for i in range(3):
            assert not shift.evaluate(10 * H + 16 + 5 * i, 12 * H, 30.0)
        assert shift.start == 10 * H

    def test_without_a_rise_it_can_still_move_later(self):
        shift = ChargeStartShift(day=DAY, s0=12 * H, start=9 * H)
        for i in range(3):
            shift.evaluate(9 * H + 1 + 5 * i, 11 * H, 25.0)
        assert shift.start == 11 * H and not shift.locked

    def test_now_after_the_start_struck_is_no_move(self):
        """14.10.2026: start 08:00, cloudy, SoC flat, candidate = now each tick.

        Before the fix the start crept to now - 10 min every 5 minutes.
        """
        shift = ChargeStartShift(day=DAY, s0=11 * H, start=8 * H)
        for i in range(12):
            now = 8 * H + 1 + 5 * i
            assert not shift.evaluate(now, now, 55.0)
        assert shift.start == 8 * H
        assert shift.soc_at_start == 55.0

    def test_a_later_start_ahead_still_moves_after_the_start_struck(self):
        shift = ChargeStartShift(day=DAY, s0=11 * H, start=8 * H)
        for i in range(3):
            shift.evaluate(8 * H + 1 + 5 * i, 10 * H, 55.0)
        assert shift.start == 10 * H

    def test_restart_keeps_a_locked_start(self):
        shift = ChargeStartShift(
            day=DAY, s0=12 * H, start=9 * H, struck_start=9 * H, locked=True
        )
        assert not shift.restart(11 * H)
        assert shift.start == 9 * H

    def test_restart_goes_back_to_the_new_s0(self):
        shift = ChargeStartShift(day=DAY, s0=12 * H, start=9 * H)
        assert shift.restart(11 * H)
        assert (shift.s0, shift.start, shift.recent) == (11 * H, 11 * H, ())


def test_round_trip_keeps_the_lock_baseline():
    shift = ChargeStartShift(
        day=DAY, s0=12 * H, start=9 * H, struck_start=9 * H, soc_at_start=33.0
    )
    restored = ChargeStartShift.from_dict(shift.to_dict())
    assert restored is not None
    assert restored.to_dict() == shift.to_dict()
    assert ChargeStartShift.from_dict({"day": "garbage"}) is None
    assert ChargeStartShift.from_dict(None) is None


_PARITY = [
    json.loads(line)
    for line in (
        Path(__file__).parent.parent / "fixtures" / "charge_start_shift_parity.jsonl"
    )
    .read_text()
    .splitlines()
]


@pytest.mark.parametrize(
    "case", _PARITY, ids=[f"{c['day']}-{c['now']}" for c in _PARITY]
)
def test_parity_with_the_backtest(case):
    """Same candidate as `shift6.py` for every evaluation of 9-12.10.2026."""
    cons = case["cons_hourly"]
    profile = SurplusProfile.build(
        DAY,
        dict(enumerate(case["pv_slots"])),
        slots_from_hourly(dict(enumerate(cons))) if cons is not None else None,
    )
    got = candidate_start(
        profile, case["prices"], case["now"], case["need"], case["s0"]
    )
    assert got == case["expected"]


class TestFullChargeMinute:
    """When the battery is forecast full from the charge start (17.10.2026)."""

    @staticmethod
    def _profile(rate_by_hour: dict[int, float]):
        from custom_components.smart_rce.domain.pv_surplus import (  # noqa: PLC0415
            SurplusProfile,
            slots_from_hourly,
        )

        return SurplusProfile.build(
            date(2026, 10, 17),
            slots_from_hourly(rate_by_hour),
            dict.fromkeys(range(48), 0.0),
        )

    def test_17_10_forecast_fills_from_22_percent_around_13_17(self):
        from custom_components.smart_rce.domain.charge_start_shift import (  # noqa: PLC0415
            full_charge_minute,
        )

        surplus = {11: 4.151, 12: 4.925, 13: 3.477, 14: 3.666, 15: 2.456}
        full = full_charge_minute(self._profile(surplus), 11 * 60, 22.0, 16 * 60)
        assert full is not None
        assert 13 * 60 + 10 <= full <= 13 * 60 + 25

    def test_not_full_before_the_end_is_none(self):
        from custom_components.smart_rce.domain.charge_start_shift import (  # noqa: PLC0415
            full_charge_minute,
        )

        weak = dict.fromkeys(range(11, 16), 1.0)
        assert full_charge_minute(self._profile(weak), 11 * 60, 22.0, 16 * 60) is None

    def test_pace_caps_a_huge_surplus_at_the_charge_rate(self):
        from custom_components.smart_rce.domain.charge_start_shift import (  # noqa: PLC0415
            full_charge_minute,
        )

        huge = dict.fromkeys(range(11, 16), 50.0)
        full = full_charge_minute(self._profile(huge), 11 * 60, 22.0, 16 * 60)
        # 22->89 at 75 s/pp + plateau 2 x 390 + top 3 x 40 + 4 x 80 + 2 x 320.
        expected = 11 * 60 + (67 * 75 + 2 * 390 + 3 * 40 + 4 * 80 + 2 * 320) / 60
        # Minute steps take the pace of the level they start at: within ~1 step per zone.
        assert full == pytest.approx(expected, abs=4)
