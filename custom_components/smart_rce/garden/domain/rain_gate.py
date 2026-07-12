"""Rain gate — keeps the mower parked (via non-work) while the grass is wet.

The mower autonomously resumes a paused task — both when its non-work window
ends AND when it finishes charging mid-task during working hours (confirmed
2026-07-09). Neither path consults the planner's `should_start`, so the only
lever we have over the firmware is the device non-work window. `RainGate` owns a
temporary `override` of that window — a block `[now − MARGIN, dry_at]` — and the
gate service pushes it to the device, restoring the user target once it is no
longer needed.

`dry_at` is LIVE (it stays in the future the whole time it rains — see
`RainState.last_wet_at`), so it is the single source of "wet until when"; the
gate needs neither `currently_wet` nor `dry_hours`.

The block is asserted ONLY where the mower would otherwise leave the dock into
wet grass — and only with a paused task to resume (`docked_with_task`):
- working hours → the charge-complete auto-resume;
- within `MARGIN` of the morning quiet-end, IF still wet past it (`dry_at > end`;
  otherwise the target end already covers the dry-out).
Everywhere else the real non-work window parks the mower, so we restore the user
target. One shape: because the block start (`now − MARGIN`) is in the past it
survives the morning boundary without a rewrite — it just keeps holding until
dry. `MARGIN` doubles as a clock-skew buffer: the block starts a touch early and
the restore is deferred until `MARGIN` past the evening start, so neither edge
races a lagging device clock.

Anti-churn: `dry_at` creeps ~1 min per tick while it keeps raining. A held block
is left untouched while its end is still more than `MARGIN` ahead of `now`, and
refreshed only as it nears expiry — where the refresh re-asserts the current
`dry_at`, so the mower never reaches the end while wet (≈ one write per
`dry_hours − MARGIN` of continuous rain, not one per tick).

State is in-memory (not persisted): a mid-block HA restart forgets `override`;
the next `evaluate` re-derives it, and `binary_sensor.luba_non_work_drift`
(un-muted once not holding) surfaces any lingering device mismatch for a manual
restore via the push button.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import ClassVar

from custom_components.smart_rce.garden.domain.non_work import (
    NonWorkHours,
    next_occurrence,
)


@dataclass
class RainGate:
    """Owns the rain override of the device non-work window (mutator-returns-bool)."""

    # Slack kept around every non-work edge: how early before a boundary we act,
    # and the clock-skew buffer at the block's own start/restore edges.
    MARGIN: ClassVar[timedelta] = timedelta(minutes=15)
    # After a manual clear we suppress re-blocking for this long so the mower has
    # time to physically leave the dock before the next tick re-reads
    # `docked_with_task` — otherwise it would re-block while still docked + wet,
    # making the clear button a no-op (cloud round-trip lags the undock).
    MANUAL_RELEASE_GRACE: ClassVar[timedelta] = timedelta(minutes=20)

    override: NonWorkHours | None = None
    _suppress_until: datetime | None = field(default=None, init=False)

    @property
    def is_holding(self) -> bool:
        """True while we override the device non-work window (gate active)."""
        return self.override is not None

    def release(self, now: datetime) -> bool:
        """Manually drop the override + suppress re-blocking for the grace window.

        The suppression lets the mower undock before the next `evaluate` re-reads
        `docked_with_task`; once it is off the dock the block naturally stops
        applying. After the grace expires (still docked + wet) it re-asserts.
        """
        self._suppress_until = now + self.MANUAL_RELEASE_GRACE
        return self._clear()

    def evaluate(
        self,
        now: datetime,
        user_target: NonWorkHours | None,
        dry_at: datetime | None,
        docked_with_task: bool,
    ) -> bool:
        """Recompute the override. Returns True if it changed (→ push + notify)."""
        if (
            not self._is_suppressed(now)
            and docked_with_task
            and user_target is not None
            and dry_at is not None
            and dry_at > now
            and self._block_applies(now, user_target, dry_at)
        ):
            return self._block(now, dry_at)
        return self._release_to_target(now, user_target)

    def _is_suppressed(self, now: datetime) -> bool:
        """Whether a recent manual clear still suppresses re-blocking."""
        return self._suppress_until is not None and now < self._suppress_until

    def _block_applies(
        self, now: datetime, user_target: NonWorkHours, dry_at: datetime
    ) -> bool:
        """Whether the mower would otherwise leave the dock into wet grass now."""
        active_end = user_target.end_of_active_window(now)
        if active_end is None:
            return True  # working hours + wet → charge-complete auto-resume risk
        # Inside the quiet window: only the morning end lets the mower out, and
        # only if the grass is still wet past it (else the target end covers it).
        return active_end - now <= self.MARGIN and dry_at > active_end

    def _block(self, now: datetime, dry_at: datetime) -> bool:
        if self.override is not None:
            # Continuing a block. Skip while its end is comfortably ahead
            # (dry_at creeps ~1 min/tick); refresh only near expiry, where it
            # re-asserts the current dry_at — so the mower never reaches the end
            # while still wet.
            if next_occurrence(now, self.override.end) - now > self.MARGIN:
                return False
            start = self.override.start  # pinned across ticks
        else:
            # Fresh block — start a touch in the past so a lagging device clock
            # still sees `now` inside the window (no start-boundary race).
            start = (now - self.MARGIN).time()
        return self._set_override(NonWorkHours(start, dry_at.time()))

    def _release_to_target(
        self, now: datetime, user_target: NonWorkHours | None
    ) -> bool:
        # Restore the user target — but keep a held block a touch longer just past
        # the evening start: it still covers `now` there, whereas flipping to the
        # plain target at the boundary could race a lagging device clock (mirror
        # of the block start buffer).
        if (
            self.override is not None
            and user_target is not None
            and user_target.end_of_active_window(now) is not None
            and now - user_target.recent_start(now) < self.MARGIN
        ):
            return False
        return self._clear()

    def _set_override(self, desired: NonWorkHours) -> bool:
        if self.override == desired:
            return False
        self.override = desired
        return True

    def _clear(self) -> bool:
        if self.override is None:
            return False
        self.override = None
        return True
