"""SnapshotSchedule — decides when a forecast snapshot is worth taking.

The valuable moment is right after a successful Solcast fetch: both sources
(Solcast and the 5-minute weather refresh) are fresh then. A half-hourly
grid is only a fallback for when no fetch-driven snapshot arrived (failed
fetch, no fetch planned for that half hour).

Two rules keep the archive at one record per half hour in the normal case:
- a fetch is new only when the "last API update" value differs from the one
  already archived and is itself recent — a Core restart re-emits the old
  value, sometimes via `unknown` so it even looks like a change;
- the grid stays quiet while a fetch-driven snapshot is pending or was taken
  recently, so it never races the fetch it is a fallback for.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Final

_UNUSABLE_STATES: Final = frozenset({"unknown", "unavailable", ""})


class SnapshotSchedule:
    """Remembers the last fetch and snapshot; answers "take one now?"."""

    # Grid fires at :11:30 / :41:30 (factory); first and last slot of the day.
    GRID_FIRST: Final = time(5, 41)
    GRID_LAST: Final = time(16, 11)
    # A snapshot younger than this makes the grid slot redundant.
    RECENT: Final = timedelta(minutes=25)
    # A real fetch reports a timestamp from moments ago; an older one is a
    # restored value showing up after a restart.
    FETCH_MAX_AGE: Final = timedelta(minutes=15)

    def __init__(self) -> None:
        self._last_fetched_at: str | None = None
        self._last_taken_at: datetime | None = None
        self._fetch_pending: bool = False

    def resume(
        self, *, last_taken_at: datetime | None, last_fetched_at: str | None
    ) -> None:
        """Pick up where the archive left off (after a restart)."""
        self._last_taken_at = last_taken_at
        self._last_fetched_at = last_fetched_at

    def remember_fetch(self, fetched_at: str | None) -> None:
        """Note a value that is not a new fetch (sensor (re)created at startup)."""
        if fetched_at is not None and fetched_at not in _UNUSABLE_STATES:
            self._last_fetched_at = fetched_at

    def accept_fetch(self, fetched_at: str | None, now: datetime) -> bool:
        """Register a "last API update" value; True when it is a new fetch.

        A new fetch marks a snapshot as pending until `fetch_handled` is called.
        """
        if fetched_at is None or fetched_at in _UNUSABLE_STATES:
            return False
        if fetched_at == self._last_fetched_at:
            return False
        self._last_fetched_at = fetched_at
        if self._is_stale(fetched_at, now):
            return False
        self._fetch_pending = True
        return True

    @classmethod
    def _is_stale(cls, fetched_at: str, now: datetime) -> bool:
        """Say whether a parsable timestamp is older than `FETCH_MAX_AGE`."""
        try:
            moment = datetime.fromisoformat(fetched_at)
        except ValueError:
            return False  # unknown format: trust the change itself
        if moment.tzinfo is None:
            return False
        return now - moment > cls.FETCH_MAX_AGE

    def grid_due(self, now: datetime) -> bool:
        """Say whether the grid slot at `now` should write a fallback snapshot."""
        slot = now.time().replace(second=0, microsecond=0)
        if not self.GRID_FIRST <= slot <= self.GRID_LAST:
            return False
        if self._fetch_pending:
            return False
        return self._last_taken_at is None or now - self._last_taken_at >= self.RECENT

    def taken(self, now: datetime) -> None:
        """Record a written snapshot."""
        self._last_taken_at = now

    def fetch_handled(self) -> None:
        """Clear the pending fetch once its snapshot was attempted (written or not)."""
        self._fetch_pending = False
