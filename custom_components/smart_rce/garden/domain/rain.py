"""Rain timing — when did it last rain, and when is the grass dry enough to mow.

`RainState` is the garden-owned aggregate (persisted via repository). It records
`rain_ended_at` — the timestamp of the last wet→dry transition (NOT a rolling
"last wet moment") — plus the `dry_hours` dry-out policy (user-configurable via
a number entity). `dry_at` derives the moment the grass is considered dry:
`rain_ended_at + dry_hours`. The planner clamps its mowing window so it never
starts before `dry_at`.

Replaces the legacy Jinja `input_datetime.luba_notify_mute_until` mechanism
(which stored the derived mute time and rolled it forward every 5 min). Here we
store the fundamental observation (rain end) and keep the policy (hours) explicit
and tunable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

DEFAULT_DRY_HOURS = 5.0


@dataclass
class RainState:
    """Mutable aggregate — last rain-end + dry-out policy, persisted via repo.

    `rain_ended_at` is `None` until the first observed wet→dry transition
    (fresh install, or never rained since). Serialization: `datetime` via
    `isoformat`/`fromisoformat` (tz-aware, HA convention); `dry_hours` is plain
    float (Store persists JSON).

    `is_wet` is the last observed wet state (drives the grass-wet sensor and
    the rain gate). It is TRANSIENT — re-derived from the weather entity on
    every observation, so it is deliberately excluded from `to_dict` (we
    persist the derived fact `rain_ended_at`, not the volatile observation).
    """

    rain_ended_at: datetime | None = None
    dry_hours: float = DEFAULT_DRY_HOURS
    is_wet: bool = False

    def observe(self, currently_wet: bool, now: datetime) -> bool:
        """Feed a wetness observation; stamp rain end on the wet→dry edge.

        Returns True if anything observable changed (the wet flag flipped or
        `rain_ended_at` advanced) so the service can refresh entities. Only a
        wet→dry edge advances `rain_ended_at` — the dry-out clock starts when
        rain ENDS, not while it falls. `is_wet` starts False, so a dry first
        reading is a no-op and a wet first reading only arms the eventual edge.
        """
        changed = self.is_wet != currently_wet
        if self.is_wet and not currently_wet:
            changed |= self.record_dry_transition(now)
        self.is_wet = currently_wet
        return changed

    def record_dry_transition(self, now: datetime) -> bool:
        """Mark that rain just ended (wet→dry). Returns True if it changed."""
        if self.rain_ended_at == now:
            return False
        self.rain_ended_at = now
        return True

    def set_dry_hours(self, hours: float) -> bool:
        """Set the dry-out policy. Returns True if it changed."""
        if hours == self.dry_hours:
            return False
        self.dry_hours = hours
        return True

    @property
    def dry_at(self) -> datetime | None:
        """When the grass is considered dry: rain_ended_at + dry_hours.

        `None` when no rain end is on record (treat as already dry).
        """
        if self.rain_ended_at is None:
            return None
        return self.rain_ended_at + timedelta(hours=self.dry_hours)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rain_ended_at": (
                self.rain_ended_at.isoformat() if self.rain_ended_at else None
            ),
            "dry_hours": self.dry_hours,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RainState:
        raw = data.get("rain_ended_at")
        ended = datetime.fromisoformat(raw) if isinstance(raw, str) else None
        hours = data.get("dry_hours", DEFAULT_DRY_HOURS)
        return cls(rain_ended_at=ended, dry_hours=float(hours))
