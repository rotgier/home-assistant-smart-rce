"""Non-work hours — the recurring quiet window when the mower must not run.

`NonWorkSchedule` is the garden-owned aggregate (persisted via repository, HA =
source of truth); `NonWorkHours` is the immutable value it carries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Any


@dataclass
class NonWorkSchedule:
    """Mutable aggregate — the garden-owned non-work target, persisted via repo.

    `target` is `None` before the first seed (analog to a not-yet-read cache).
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
