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
