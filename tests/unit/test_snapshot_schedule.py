"""SnapshotSchedule: one snapshot per half hour, fetch-driven first, grid as fallback."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.smart_rce.domain.pv_forecast.snapshot_schedule import (
    SnapshotSchedule,
)
import pytest

_TZ = ZoneInfo("Europe/Warsaw")


def _at(hour: int, minute: int, second: int = 30) -> datetime:
    return datetime(2026, 10, 14, hour, minute, second, tzinfo=_TZ)


# 08:10:40 local = 06:10:40 UTC, seconds after the 06:10:37 UTC fetch.
_FETCH_NOW = _at(8, 10, 40)
_AFTER_SECOND = _at(8, 40, 45)


def test_new_fetch_is_accepted_once() -> None:
    schedule = SnapshotSchedule()
    assert schedule.accept_fetch("2026-10-14T06:10:37+00:00", _FETCH_NOW)
    assert not schedule.accept_fetch("2026-10-14T06:10:37+00:00", _FETCH_NOW)
    assert schedule.accept_fetch("2026-10-14T06:40:40+00:00", _AFTER_SECOND)


@pytest.mark.parametrize("value", [None, "unknown", "unavailable", ""])
def test_unusable_value_is_not_a_fetch(value: str | None) -> None:
    assert not SnapshotSchedule().accept_fetch(value, _FETCH_NOW)


def test_value_remembered_at_startup_is_not_a_fetch() -> None:
    schedule = SnapshotSchedule()
    schedule.remember_fetch("2026-10-14T05:40:44+00:00")
    assert not schedule.accept_fetch("2026-10-14T05:40:44+00:00", _FETCH_NOW)


def test_resume_from_archive_blocks_the_restart_re_emit() -> None:
    schedule = SnapshotSchedule()
    schedule.resume(last_taken_at=_at(9, 11), last_fetched_at="v1")
    assert not schedule.accept_fetch("v1", _FETCH_NOW)
    assert not schedule.grid_due(_at(9, 11, 40))


def test_grid_due_without_any_snapshot_inside_the_window() -> None:
    schedule = SnapshotSchedule()
    assert schedule.grid_due(_at(5, 41))
    assert schedule.grid_due(_at(16, 11))


@pytest.mark.parametrize(("hour", "minute"), [(5, 11), (16, 41), (20, 11), (0, 41)])
def test_grid_quiet_outside_the_window(hour: int, minute: int) -> None:
    assert not SnapshotSchedule().grid_due(_at(hour, minute))


def test_grid_waits_for_a_pending_fetch_snapshot() -> None:
    """The fetch landed, its snapshot still counts down — the grid must not race it."""
    schedule = SnapshotSchedule()
    schedule.accept_fetch("v1", _FETCH_NOW)
    assert not schedule.grid_due(_at(9, 11))

    schedule.taken(_at(9, 11, 5))
    schedule.fetch_handled()
    assert not schedule.grid_due(_at(9, 11))  # recent snapshot


def test_grid_fills_the_half_hour_a_fetch_failed() -> None:
    schedule = SnapshotSchedule()
    schedule.taken(_at(8, 11, 5))  # last good fetch-driven snapshot
    assert schedule.grid_due(_at(8, 41))  # 08:40 fetch failed: nothing since


def test_recent_threshold() -> None:
    schedule = SnapshotSchedule()
    taken = _at(9, 0, 0)
    schedule.taken(taken)
    assert not schedule.grid_due(taken + SnapshotSchedule.RECENT - timedelta(seconds=1))
    assert schedule.grid_due(taken + SnapshotSchedule.RECENT)


def test_handled_fetch_without_snapshot_releases_the_grid() -> None:
    """No dispatch yet → nothing written; the grid must not stay blocked forever."""
    schedule = SnapshotSchedule()
    schedule.accept_fetch("v1", _FETCH_NOW)
    schedule.fetch_handled()
    assert schedule.grid_due(_at(9, 11))


def test_old_timestamp_after_restart_is_not_a_fetch() -> None:
    """Startup: unknown → restored timestamp from hours ago looks like a change."""
    schedule = SnapshotSchedule()
    assert not schedule.accept_fetch("2026-10-13T18:01:41+00:00", _FETCH_NOW)
    assert schedule.grid_due(_at(8, 11))  # nothing pending
    assert not schedule.accept_fetch("2026-10-13T18:01:41+00:00", _FETCH_NOW)


def test_unparsable_value_falls_back_to_change_detection() -> None:
    schedule = SnapshotSchedule()
    assert schedule.accept_fetch("v1", _FETCH_NOW)
