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
from typing import Any, ClassVar


@dataclass
class RainState:
    """Mutable aggregate — last rain-end + dry-out policy, persisted via repo.

    `rain_ended_at` is `None` until the first confirmed wet→dry transition
    (fresh install, or never rained since). Serialization: `datetime` via
    `isoformat`/`fromisoformat` (tz-aware, HA convention); `dry_hours` is plain
    float (Store persists JSON).

    `is_wet` is the CONFIRMED wet state (drives the grass-wet sensor and the
    rain gate), `wet_since` is when the raw reading first turned wet, and
    `last_wet_at` is the latest confirmed-wet moment (anchors `dry_at` WHILE it
    is raining — `rain_ended_at` still holds the PREVIOUS rain's end mid-shower).
    All three are TRANSIENT — re-derived from the weather entity on every
    observation, so they are deliberately excluded from `to_dict` (we persist
    the derived fact `rain_ended_at`, not the volatile observation).
    """

    WET_DWELL: ClassVar[timedelta] = timedelta(minutes=10)  # rain must persist
    _DEFAULT_DRY_HOURS: ClassVar[float] = 5.0

    rain_ended_at: datetime | None = None
    dry_hours: float = _DEFAULT_DRY_HOURS
    is_wet: bool = False
    wet_since: datetime | None = None
    last_wet_at: datetime | None = None

    def observe(self, raw_wet: bool, now: datetime) -> bool:
        """Feed a raw wetness reading; confirm wet only after WET_DWELL of rain.

        A brief shower (a few drops) trips the raw reading but never wets the
        grass, so `is_wet` — the confirmed state consumed by the sensor, gate
        and rain-end stamp — flips True only once raw rain has PERSISTED longer
        than `WET_DWELL`. `wet_since` tracks the current raw-wet streak (reset
        the moment it reads dry). Returns True if anything observable changed.
        Only a confirmed wet→dry edge advances `rain_ended_at` — the dry-out
        clock starts when real rain ENDS, not while a passing shower clears.
        """
        if raw_wet:
            if self.wet_since is None:
                self.wet_since = now
            confirmed = now - self.wet_since > self.WET_DWELL
        else:
            self.wet_since = None
            confirmed = False
        changed = self.is_wet != confirmed
        if self.is_wet and not confirmed:
            changed |= self._record_dry_transition(now)
        self.is_wet = confirmed
        if confirmed:
            self.last_wet_at = now  # anchor dry_at forward while it rains
        return changed

    def _record_dry_transition(self, now: datetime) -> bool:
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
        """When the grass is considered dry.

        While confirmed wet the rain is still falling, so the dry-out clock has
        not started — anchor on `last_wet_at` (latest wet observation) so
        `dry_at` stays in the future (`last_wet_at + dry_hours`) and the planner
        keeps its window closed. `rain_ended_at` alone would be STALE here (it
        holds the previous rain's end until this shower clears), which let the
        planner open the window and resume into wet grass (2026-07-09). Once
        dry, anchor on `rain_ended_at` for a fixed dry-out deadline. `None` when
        no rain is on record (treat as already dry).
        """
        base = self.last_wet_at if self.is_wet else self.rain_ended_at
        if base is None:
            return None
        return base + timedelta(hours=self.dry_hours)

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
        hours = data.get("dry_hours", cls._DEFAULT_DRY_HOURS)
        return cls(rain_ended_at=ended, dry_hours=float(hours))
