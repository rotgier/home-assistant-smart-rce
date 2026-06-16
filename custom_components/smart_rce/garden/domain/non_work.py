"""Non-work hours — the recurring quiet window when the mower must not run.

`NonWorkSchedule` is the garden-owned aggregate (persisted via repository, HA =
source of truth); `NonWorkHours` is the immutable value it carries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

_ONE_DAY = timedelta(days=1)


@dataclass
class NonWorkSchedule:
    """Mutable aggregate — the garden-owned non-work target, persisted via repo.

    `target` is `None` until the user sets it (fresh install, nothing restored).
    Serialization mirrors `BatteryChargePolicy`: `time` via `isoformat` /
    `fromisoformat` (Store persists JSON, `time` is not JSON-native).
    """

    target: NonWorkHours | None = None

    def set_target(self, hours: NonWorkHours | None) -> bool:
        """Set the target; returns True when it actually changed (mutator style)."""
        if hours == self.target:
            return False
        self.target = hours
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.target.start.isoformat() if self.target else None,
            "end": self.target.end.isoformat() if self.target else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NonWorkSchedule:
        start, end = data.get("start"), data.get("end")
        if isinstance(start, str) and isinstance(end, str):
            return cls(NonWorkHours(time.fromisoformat(start), time.fromisoformat(end)))
        return cls(None)


@dataclass(frozen=True)
class NonWorkHours:
    """Recurring daily quiet window when the mower must not run.

    `start`/`end` are wall-clock times. `end` may be earlier than `start` when
    the window crosses midnight (e.g. 20:35 → 10:05).
    """

    start: time
    end: time

    def next_start(self, now: datetime) -> datetime:
        """Upcoming quiet-window start: today at `start`, or tomorrow if past."""
        return self._next_occurrence(now, self.start)

    def end_of_active_window(self, now: datetime) -> datetime | None:
        """When the currently-active quiet window ends; None when not inside.

        Handles the midnight-crossing window (start > end): inside when the
        time of day is past `start` OR before `end`.
        """
        tod = now.time()
        if self.start <= self.end:
            inside = self.start <= tod < self.end
        else:
            inside = tod >= self.start or tod < self.end
        if not inside:
            return None
        return self._next_occurrence(now, self.end)

    def recent_end(self, now: datetime) -> datetime:
        """Most recent occurrence of `end` at or before now (today, else yesterday).

        A stable anchor for "the quiet window just ended" that survives leaving
        the window, unlike `end_of_active_window` (which is None once outside).
        """
        candidate = now.replace(
            hour=self.end.hour, minute=self.end.minute, second=0, microsecond=0
        )
        return candidate if candidate <= now else candidate - _ONE_DAY

    @staticmethod
    def _next_occurrence(now: datetime, tod: time) -> datetime:
        """Next datetime at wall-clock `tod` that is >= now (today, else tomorrow)."""
        candidate = now.replace(
            hour=tod.hour, minute=tod.minute, second=0, microsecond=0
        )
        return candidate if candidate >= now else candidate + _ONE_DAY
