"""Mowing hold — keeps the mower parked (via non-work) for rain OR a manual park.

The mower autonomously resumes a paused task — both when its non-work window
ends AND when it finishes charging mid-task during working hours (confirmed
2026-07-09). Neither path consults the planner's `should_start`, so the only
lever we have over the firmware is the device non-work window. `MowingHold` owns
a temporary `override` of that window and the service pushes it to the device,
restoring the user target once no hold is active.

Two independent hold reasons, OR-combined — the mower stays parked until BOTH
clear, so the effective override end is the LATEST of the active ends:

- **rain** — derived live each `evaluate`: docked-with-task AND grass not dry
  (`dry_at > now`) AND the mower would otherwise leave (`_hold_applies`). Ends at
  `dry_at` (LIVE; stays in the future while it rains — see `RainState.dry_at`).
  Not persisted (re-derived). A manual `clear` suppresses it for
  `MANUAL_RELEASE_GRACE` (grass is actually fine, resume now).
- **manual** — `manual_until` deadline set by the dashboard park button. Ends at
  `manual_until`. Independent of `docked_with_task` and of rain (a manual clear
  does NOT drop it; only expiry or the cancel button). PERSISTED (`to_dict`), so
  a restart mid-park does not release the mower into the kids' football game.

The hold shape is `[now − MARGIN, end]`. Because the start (`now − MARGIN`) is in
the past it survives the morning boundary without a rewrite. `MARGIN` doubles as
a clock-skew buffer at the start/restore edges. Anti-churn: on tick-driven
`evaluate` a held window is left untouched while its end is still more than
`MARGIN` ahead of `now` (`dry_at` creeps ~1 min/tick) — refreshed only near
expiry, where it re-asserts the current latest end (so it never expires while a
reason is active, and a rising manual/rain end is picked up before the device
window is reached). A user action (`park`/`cancel`/`clear`) passes `force=True`
to apply immediately, bypassing the skip.

Only `manual_until` persists; `override` and the rain-suppression window are
transient (re-derived on the next `evaluate`).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from custom_components.smart_rce.garden.domain.non_work import (
    NonWorkHours,
    next_occurrence,
)


class MowingHold:
    """Owns the device non-work override for rain + manual holds (mutator-bool).

    A DDD entity (mutable state + behaviour + persisted), so a plain class.
    """

    # Slack kept around every non-work edge: how early before a boundary we act,
    # and the clock-skew buffer at the hold's own start/restore edges.
    MARGIN = timedelta(minutes=15)
    # After a manual clear we suppress re-holding on RAIN for this long so the
    # mower has time to physically leave the dock before the next tick re-reads
    # `docked_with_task` — otherwise it would re-hold while still docked + wet,
    # making the clear button a no-op (cloud round-trip lags the undock).
    MANUAL_RELEASE_GRACE = timedelta(minutes=20)

    def __init__(
        self,
        override: NonWorkHours | None = None,
        manual_until: datetime | None = None,
        manual_since: datetime | None = None,
    ) -> None:
        self.override = override
        self.manual_until = manual_until  # persisted manual-park deadline
        self.manual_since = manual_since  # persisted manual-park start (for display)
        self._suppress_until: datetime | None = None  # transient rain suppression

    @property
    def is_holding(self) -> bool:
        """True while we override the device non-work window (any hold active)."""
        return self.override is not None

    def set_manual(self, now: datetime, minutes: int) -> bool:
        """Arm a manual park until `now + minutes`. Returns True if it changed."""
        until = now + timedelta(minutes=minutes)
        if self.manual_until == until:
            return False
        self.manual_until = until
        self.manual_since = now
        return True

    def cancel_manual(self) -> bool:
        """Drop the manual park. Returns True if one was armed."""
        if self.manual_until is None:
            return False
        self.manual_until = None
        self.manual_since = None
        return True

    def suppress_rain(self, now: datetime) -> None:
        """Suppress the RAIN reason for the grace window (manual clear button).

        Lets the mower undock before the next `evaluate` re-reads
        `docked_with_task`; does NOT touch the manual park.
        """
        self._suppress_until = now + self.MANUAL_RELEASE_GRACE

    def evaluate(
        self,
        now: datetime,
        user_target: NonWorkHours | None,
        dry_at: datetime | None,
        docked_with_task: bool,
        *,
        force: bool = False,
    ) -> bool:
        """Recompute the override. Returns True if it changed (→ push + notify).

        `force` (user action) applies the new window immediately, bypassing the
        tick-driven anti-churn skip.
        """
        end = self._desired_end(now, user_target, dry_at, docked_with_task)
        if end is None:
            return self._release_to_target(now, user_target)
        return self._hold(now, end, force=force)

    def _desired_end(
        self,
        now: datetime,
        user_target: NonWorkHours | None,
        dry_at: datetime | None,
        docked_with_task: bool,
    ) -> datetime | None:
        """Latest 'keep parked until' across active holds; None if none active."""
        ends: list[datetime] = []
        if self.manual_until is not None and now < self.manual_until:
            ends.append(self.manual_until)
        if (
            not self._is_suppressed(now)
            and docked_with_task
            and user_target is not None
            and dry_at is not None
            and dry_at > now
            and self._hold_applies(now, user_target, dry_at)
        ):
            ends.append(dry_at)
        return max(ends) if ends else None

    def _is_suppressed(self, now: datetime) -> bool:
        """Whether a recent manual clear still suppresses the rain reason."""
        return self._suppress_until is not None and now < self._suppress_until

    def _hold_applies(
        self, now: datetime, user_target: NonWorkHours, dry_at: datetime
    ) -> bool:
        """Whether the mower would otherwise leave the dock into wet grass now."""
        active_end = user_target.end_of_active_window(now)
        if active_end is None:
            return True  # working hours + wet → charge-complete auto-resume risk
        # Inside the quiet window: only the morning end lets the mower out, and
        # only if the grass is still wet past it (else the target end covers it).
        return active_end - now <= self.MARGIN and dry_at > active_end

    def _hold(self, now: datetime, end: datetime, *, force: bool) -> bool:
        if self.override is not None:
            # Continuing a hold. On a tick, skip while the end is comfortably
            # ahead (dry_at creeps ~1 min/tick); refresh only near expiry, where
            # it re-asserts the current latest end. A user action forces through.
            if (
                not force
                and next_occurrence(now, self.override.end) - now > self.MARGIN
            ):
                return False
            start = self.override.start  # pinned across ticks
        else:
            # Fresh hold — start a touch in the past so a lagging device clock
            # still sees `now` inside the window (no start-boundary race).
            start = (now - self.MARGIN).time()
        return self._set_override(NonWorkHours(start, end.time()))

    def _release_to_target(
        self, now: datetime, user_target: NonWorkHours | None
    ) -> bool:
        # Restore the user target — but keep a held window a touch longer just past
        # the evening start: it still covers `now` there, whereas flipping to the
        # plain target at the boundary could race a lagging device clock (mirror
        # of the hold start buffer).
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "manual_until": (
                self.manual_until.isoformat() if self.manual_until else None
            ),
            "manual_since": (
                self.manual_since.isoformat() if self.manual_since else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MowingHold:
        until = _parse_dt(data.get("manual_until"))
        since = _parse_dt(data.get("manual_since"))
        return cls(manual_until=until, manual_since=since)


def _parse_dt(raw: object) -> datetime | None:
    return datetime.fromisoformat(raw) if isinstance(raw, str) else None
