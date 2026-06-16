"""Unit tests for the NonWorkSchedule aggregate (serialization + set_target)."""

from datetime import time

from custom_components.smart_rce.garden.domain.non_work import (
    NonWorkHours,
    NonWorkSchedule,
)


def test_round_trip_with_target() -> None:
    schedule = NonWorkSchedule(NonWorkHours(time(20, 35), time(10, 5)))

    assert NonWorkSchedule.from_dict(schedule.to_dict()) == schedule


def test_to_dict_with_no_target() -> None:
    assert NonWorkSchedule().to_dict() == {"start": None, "end": None}


def test_from_dict_none_and_missing_yield_no_target() -> None:
    assert NonWorkSchedule.from_dict({"start": None, "end": None}).target is None
    assert NonWorkSchedule.from_dict({}).target is None


def test_set_target_returns_changed_flag() -> None:
    schedule = NonWorkSchedule()
    hours = NonWorkHours(time(20, 35), time(10, 5))

    assert schedule.set_target(hours) is True
    assert schedule.target == hours
    assert schedule.set_target(hours) is False  # same value → no change


def test_set_target_to_none() -> None:
    schedule = NonWorkSchedule(NonWorkHours(time(20, 35), time(10, 5)))

    assert schedule.set_target(None) is True
    assert schedule.target is None


# --- NonWorkHours calendar behavior (rich domain — moved out of the service) ---

from datetime import datetime, timedelta  # noqa: E402

NOON = datetime(2026, 6, 12, 12, 0)
HOURS = NonWorkHours(time(20, 35), time(10, 5))  # crosses midnight


def test_next_start_today_when_ahead() -> None:
    assert HOURS.next_start(NOON) == NOON.replace(hour=20, minute=35)


def test_next_start_rolls_to_tomorrow_when_past() -> None:
    late = NOON.replace(hour=22)
    assert HOURS.next_start(late) == late.replace(hour=20, minute=35) + timedelta(
        days=1
    )


def test_end_of_active_window_none_outside() -> None:
    assert HOURS.end_of_active_window(NOON) is None


def test_end_of_active_window_evening_side() -> None:
    late = NOON.replace(hour=22)  # inside since 20:35 → ends tomorrow 10:05
    assert HOURS.end_of_active_window(late) == late.replace(
        hour=10, minute=5
    ) + timedelta(days=1)


def test_end_of_active_window_morning_side() -> None:
    morning = NOON.replace(hour=9)  # inside until 10:05 today
    assert HOURS.end_of_active_window(morning) == morning.replace(hour=10, minute=5)


def test_non_crossing_window() -> None:
    siesta = NonWorkHours(time(12, 0), time(14, 0))
    inside = NOON.replace(hour=13)
    assert siesta.end_of_active_window(inside) == inside.replace(hour=14, minute=0)
    assert siesta.end_of_active_window(NOON.replace(hour=15)) is None


def test_recent_end_today_when_past() -> None:
    assert HOURS.recent_end(NOON) == NOON.replace(hour=10, minute=5)


def test_recent_end_rolls_to_yesterday_when_ahead() -> None:
    early = NOON.replace(hour=9)  # today's 10:05 is still ahead → yesterday's
    assert HOURS.recent_end(early) == early.replace(hour=10, minute=5) - timedelta(
        days=1
    )


def test_recent_end_at_exact_end_is_today() -> None:
    at_end = NOON.replace(hour=10, minute=5)
    assert HOURS.recent_end(at_end) == at_end
