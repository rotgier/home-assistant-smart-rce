"""Rain gate — keeps the mower parked (via non-work) while the grass is wet.

The mower autonomously resumes a paused task — both when its non-work window
ends AND when it finishes charging mid-task during working hours (confirmed
2026-07-09). Neither path consults the planner's `should_start`, so the only
lever we have over the firmware is the device non-work window. `RainGate` owns
an `override` of that window and the gate service pushes it to the device,
restoring the user target once the grass is dry.

`dry_at` is LIVE (it stays in the future the whole time it rains — see
`RainState.last_wet_at`), so it is the single source of "wet until when"; the
gate needs neither `currently_wet` nor `dry_hours`. Two override shapes, by
where `now` falls:

- Inside the user quiet window, near its end (`GATE_WINDOW`) → extend the END
  past `dry_at`. Near-boundary only: extending at 22:00 for a 09:45 end would
  over-hold (grass may dry by morning).
- During working hours, ONLY when the mower is DOCKED WITH A PAUSED TASK
  (`docked_with_task`) → a temporary block `[now, dry_at]` so the charge-complete
  auto-resume cannot fire into wet grass. Gated on docked-with-task so we never
  disturb an active mow or block when there is nothing to resume.

Anti-churn: `dry_at` creeps ~1 min per tick while it keeps raining, so a naive
"end = dry_at" would re-push the window every tick and burn the 300-sends/24h
budget. Both branches instead skip re-writes while the current end is still more
than `GATE_WINDOW` ahead of `now` (the block end is `dry_at`, hours away), and
only refresh it as it nears expiry — where the refresh re-asserts the current
`dry_at`, so the mower never reaches the end while still wet. The block start is
`now − START_BUFFER` (a touch in the past, so a lagging device clock still sees
`now` inside the window) and is pinned across ticks.

State is in-memory (not persisted): a mid-block HA restart forgets `override`;
the next `evaluate` re-derives it, and `binary_sensor.luba_non_work_drift`
(un-muted once not holding) surfaces any lingering device mismatch for a manual
restore via the push button.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import ClassVar

from custom_components.smart_rce.garden.domain.non_work import (
    NonWorkHours,
    next_occurrence,
)


@dataclass
class RainGate:
    """Owns the rain override of the device non-work window (mutator-returns-bool)."""

    GATE_WINDOW: ClassVar[timedelta] = timedelta(minutes=10)  # boundary closeness
    START_BUFFER: ClassVar[timedelta] = timedelta(
        minutes=15
    )  # block start vs clock skew

    override: NonWorkHours | None = None

    @property
    def is_holding(self) -> bool:
        """True while we override the device non-work window (gate active)."""
        return self.override is not None

    def release(self) -> bool:
        """Manually drop the override (user override). Returns True if it changed.

        The next `evaluate` may re-assert if conditions still warrant it;
        releasing while dry sticks.
        """
        return self._clear()

    def evaluate(
        self,
        now: datetime,
        user_target: NonWorkHours | None,
        dry_at: datetime | None,
        docked_with_task: bool,
    ) -> bool:
        """Recompute the override. Returns True if it changed (→ push + notify)."""
        if user_target is None or dry_at is None or dry_at <= now:
            return self._clear()  # no target, or grass dry → restore target
        active_end = user_target.end_of_active_window(now)
        if active_end is not None:
            return self._extend_end(now, user_target, active_end, dry_at)
        if not docked_with_task:
            # Working hours, mower mowing or idle-done — nothing to hold back.
            return self._clear()
        return self._block(now, user_target, dry_at)

    def _extend_end(
        self,
        now: datetime,
        user_target: NonWorkHours,
        active_end: datetime,
        dry_at: datetime,
    ) -> bool:
        device_end = self._current_end(now) or active_end
        if device_end - now > self.GATE_WINDOW:
            return False  # too early — grass may dry before the boundary
        if dry_at <= active_end:
            return self._clear()  # user end already covers the dry-out
        return self._set_override(NonWorkHours(user_target.start, dry_at.time()))

    def _block(
        self, now: datetime, user_target: NonWorkHours, dry_at: datetime
    ) -> bool:
        if self.override is not None and self.override.start != user_target.start:
            # Continuing a block. Skip re-writes while its end is comfortably
            # ahead (dry_at creeps ~1 min/tick while raining); refresh only near
            # expiry, where it re-asserts the current dry_at — so the mower never
            # reaches the end while still wet.
            if next_occurrence(now, self.override.end) - now > self.GATE_WINDOW:
                return False
            start = self.override.start  # pinned across ticks
        else:
            # Fresh block — start a touch in the past (START_BUFFER) so a lagging
            # device clock still sees `now` inside the window (no start race).
            start = (now - self.START_BUFFER).time()
        return self._set_override(NonWorkHours(start, dry_at.time()))

    def _set_override(self, desired: NonWorkHours) -> bool:
        if self.override == desired:
            return False
        self.override = desired
        return True

    def _current_end(self, now: datetime) -> datetime | None:
        return next_occurrence(now, self.override.end) if self.override else None

    def _clear(self) -> bool:
        if self.override is None:
            return False
        self.override = None
        return True
