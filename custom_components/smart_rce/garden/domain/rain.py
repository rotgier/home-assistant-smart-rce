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

from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any


class RainState:
    """Mutable aggregate — last rain-end + dry-out policy, persisted via repo.

    A DDD entity (behaviour + lifecycle + persisted), so a plain class with an
    explicit `__init__`, not a dataclass — the constructor takes only the
    reconstitution inputs (`rain_ended_at`, `dry_hours`; the fields `to_dict`
    round-trips), while the observation state is private and initialised
    internally.

    `rain_ended_at` is `None` until the first confirmed wet→dry transition
    (fresh install, or never rained since). Serialization: `datetime` via
    `isoformat`/`fromisoformat` (tz-aware, HA convention); `dry_hours` is plain
    float (Store persists JSON).

    `_is_wet` is the CONFIRMED wet state (drives the grass-wet sensor and the
    mowing hold via `is_wet`), `_wet_since` is when the raw reading first turned
    wet, and `_last_wet_at` is the latest confirmed-wet moment (anchors `dry_at`
    WHILE it is raining — `rain_ended_at` still holds the PREVIOUS rain's end
    mid-shower). `_is_wet` and `_last_wet_at` ARE persisted so a restart mid-rain
    keeps `dry_at` correct; `_wet_since` stays transient (a not-yet-confirmed
    dwell is cheap to re-run on the next observation).
    """

    # Raw rain must persist ≥ this to confirm. 9 min (not 10) so ~3 consecutive
    # 5-min coordinator ticks reliably cross it despite jitter — the 3rd tick
    # lands ~10 min in, and a 1-min margin below keeps tick-2 (~5 min) out.
    WET_DWELL = timedelta(minutes=9)
    _DEFAULT_DRY_HOURS = 5.0

    def __init__(
        self,
        rain_ended_at: datetime | None = None,
        dry_hours: float = _DEFAULT_DRY_HOURS,
    ) -> None:
        self.rain_ended_at = rain_ended_at
        self.dry_hours = dry_hours
        self._is_wet = False
        self._wet_since: datetime | None = None
        self._last_wet_at: datetime | None = None

    def observe(self, raw_wet: bool, now: datetime) -> RainEvent:
        """Feed a raw wetness reading; report the resulting domain event.

        A brief shower (a few drops) trips the raw reading but never wets the
        grass, so `is_wet` — the confirmed state consumed by the sensor, gate
        and rain-end stamp — flips True only once raw rain has PERSISTED longer
        than `WET_DWELL`. `_wet_since` tracks the current raw-wet streak (reset
        the moment it reads dry). Only a confirmed wet→dry edge advances
        `rain_ended_at` — the dry-out clock starts when real rain ENDS, not
        while a passing shower clears.

        Returns a `RainEvent` describing what happened — the application service
        decides what each event means (persist / notify); the domain stays out
        of that. `STILL_RAINING` is observable (it advances `dry_at`) but touches
        no persisted field, so it notifies without a Store write.
        """
        if raw_wet:
            if self._wet_since is None:
                self._wet_since = now
            confirmed = now - self._wet_since >= self.WET_DWELL
        else:
            self._wet_since = None
            confirmed = False
        was_wet = self._is_wet
        self._is_wet = confirmed
        if confirmed:
            self._last_wet_at = now  # anchor dry_at forward while it rains
        if confirmed and not was_wet:
            return RainEvent.RAIN_CONFIRMED
        if was_wet and not confirmed:
            self._record_dry_transition(now)
            return RainEvent.RAIN_ENDED
        if confirmed:
            return RainEvent.STILL_RAINING
        return RainEvent.NONE

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

    def mark_dry(self) -> bool:
        """Force the grass dry now — user override of a false wet reading.

        Wipes the rain observation to the "no rain on record" baseline so
        `dry_at` becomes None (already dry): the planner drops its window floor
        and the mowing hold releases its rain branch. No re-confirm suppression
        — if the weather reading still reads wet, a fresh `WET_DWELL` streak
        must re-accumulate (`_wet_since` reset) before it confirms wet again.
        Returns True if anything changed.
        """
        if (
            self.rain_ended_at is None
            and not self._is_wet
            and self._last_wet_at is None
            and self._wet_since is None
        ):
            return False
        self.rain_ended_at = None
        self._is_wet = False
        self._last_wet_at = None
        self._wet_since = None
        return True

    @property
    def is_wet(self) -> bool:
        """Confirmed wet state — raw rain sustained past WET_DWELL."""
        return self._is_wet

    @property
    def dry_at(self) -> datetime | None:
        """When the grass is considered dry.

        While confirmed wet the rain is still falling, so the dry-out clock has
        not started — anchor on `_last_wet_at` (latest wet observation) so
        `dry_at` stays in the future (`_last_wet_at + dry_hours`) and the planner
        keeps its window closed. `rain_ended_at` alone would be STALE here (it
        holds the previous rain's end until this shower clears), which let the
        planner open the window and resume into wet grass (2026-07-09). Once
        dry, anchor on `rain_ended_at` for a fixed dry-out deadline. `None` when
        no rain is on record (treat as already dry).
        """
        base = self._last_wet_at if self._is_wet else self.rain_ended_at
        if base is None:
            return None
        return base + timedelta(hours=self.dry_hours)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rain_ended_at": (
                self.rain_ended_at.isoformat() if self.rain_ended_at else None
            ),
            "dry_hours": self.dry_hours,
            # Persist the confirmed-wet observation too so a restart mid-rain
            # keeps `dry_at` anchored on `last_wet_at` (future) instead of
            # falling back to a stale `rain_ended_at` until the next observe
            # re-confirms (~1 event + WET_DWELL). `_wet_since` stays transient —
            # an in-progress, not-yet-confirmed dwell is cheap to re-run.
            "is_wet": self._is_wet,
            "last_wet_at": (
                self._last_wet_at.isoformat() if self._last_wet_at else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RainState:
        raw = data.get("rain_ended_at")
        ended = datetime.fromisoformat(raw) if isinstance(raw, str) else None
        hours = data.get("dry_hours", cls._DEFAULT_DRY_HOURS)
        state = cls(rain_ended_at=ended, dry_hours=float(hours))
        state._is_wet = bool(data.get("is_wet", False))
        raw_wet_at = data.get("last_wet_at")
        if isinstance(raw_wet_at, str):
            state._last_wet_at = datetime.fromisoformat(raw_wet_at)
        return state


class RainEvent(Enum):
    """What a single `observe` produced — the application service maps meaning.

    Every non-`NONE` event changes a PERSISTED field (`is_wet`/`last_wet_at` on
    CONFIRMED/STILL_RAINING, `rain_ended_at` on ENDED — all in `to_dict`), so the
    service persists + notifies on any of them. Persisting only on `RAIN_ENDED`
    would lose the wet state across a restart mid-rain.
    """

    NONE = auto()  # nothing observable changed
    RAIN_CONFIRMED = auto()  # raw rain crossed WET_DWELL → is_wet False→True
    STILL_RAINING = auto()  # still confirmed; last_wet_at (and dry_at) advanced
    RAIN_ENDED = auto()  # is_wet True→False; rain_ended_at stamped (persist)
