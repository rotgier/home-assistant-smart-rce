"""Tests for `walk_back_workdays(today, days_back, workday_dates)`.

Pure domain function — no HA dependency. Set-based lookup is authoritative
(set sourced from `WorkdayCalendarReader`); no fallback heuristic. Tests
cover holiday-aware behavior (May 1 / May 3 in Poland) plus the explicit
no-fallback contract.
"""

from __future__ import annotations

from datetime import date

from custom_components.smart_rce.domain.pv_forecast import walk_back_workdays


def test_returns_nth_most_recent_workday() -> None:
    """Set with 5 consecutive workdays → walk_back_workdays returns them in order."""
    today = date(2026, 5, 12)  # Tue
    workdays = {
        date(2026, 5, 5),  # Tue (D-7)
        date(2026, 5, 6),  # Wed
        date(2026, 5, 7),  # Thu
        date(2026, 5, 8),  # Fri
        date(2026, 5, 11),  # Mon
    }
    assert walk_back_workdays(today, 1, workdays) == date(2026, 5, 11)
    assert walk_back_workdays(today, 2, workdays) == date(2026, 5, 8)
    assert walk_back_workdays(today, 3, workdays) == date(2026, 5, 7)
    assert walk_back_workdays(today, 4, workdays) == date(2026, 5, 6)
    assert walk_back_workdays(today, 5, workdays) == date(2026, 5, 5)


def test_skips_polish_holidays_in_may() -> None:
    """May 1 (Labor Day) is absent from workday set — holiday-awareness path.

    With Polish holidays the workday before Mon 2026-05-04 is Thu 2026-04-30
    (Fri 2026-05-01 is Labor Day, then weekend). A naive "skip weekends"
    heuristic would return 2026-05-01 as the 7th most recent workday before
    Tue 2026-05-12; calendar-derived lookup correctly returns 2026-04-30.
    """
    today = date(2026, 5, 12)
    workdays = {
        date(2026, 4, 28),  # Tue
        date(2026, 4, 29),  # Wed
        date(2026, 4, 30),  # Thu
        # 2026-05-01 Fri  Labor Day (missing)
        # 2026-05-02 Sat
        # 2026-05-03 Sun (also Constitution Day)
        date(2026, 5, 4),  # Mon
        date(2026, 5, 5),
        date(2026, 5, 6),
        date(2026, 5, 7),
        date(2026, 5, 8),
        date(2026, 5, 11),
    }
    # Sorted DESC strictly before today: 5/11, 5/8, 5/7, 5/6, 5/5, 5/4, 4/30, 4/29, 4/28
    assert walk_back_workdays(today, 7, workdays) == date(2026, 4, 30)
    assert walk_back_workdays(today, 8, workdays) == date(2026, 4, 29)


def test_empty_set_returns_none_no_fallback() -> None:
    """No fallback to 'skip weekends' — empty set surfaces config issue."""
    today = date(2026, 5, 12)
    assert walk_back_workdays(today, 1, set()) is None
    assert walk_back_workdays(today, 5, set()) is None


def test_shallower_set_than_days_back_returns_none() -> None:
    today = date(2026, 5, 12)
    workdays = {date(2026, 5, 11), date(2026, 5, 8)}
    assert walk_back_workdays(today, 1, workdays) == date(2026, 5, 11)
    assert walk_back_workdays(today, 2, workdays) == date(2026, 5, 8)
    assert walk_back_workdays(today, 3, workdays) is None


def test_excludes_today_itself_from_lookup() -> None:
    """Even if today is in the set, walk back is strictly before today."""
    today = date(2026, 5, 12)
    workdays = {today, date(2026, 5, 11), date(2026, 5, 8)}
    assert walk_back_workdays(today, 1, workdays) == date(2026, 5, 11)


def test_zero_or_negative_days_back_returns_none() -> None:
    today = date(2026, 5, 12)
    workdays = {date(2026, 5, 11)}
    assert walk_back_workdays(today, 0, workdays) is None
    assert walk_back_workdays(today, -1, workdays) is None
