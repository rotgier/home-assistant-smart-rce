"""Rain gate — holds the mower in non-work past its end while the grass is wet.

The mower auto-resumes a paused task when its non-work window ends. If the
grass is still wet at that boundary, resuming mows wet grass (the planner's
own `dry_at` clamp only gates OUR start signal, not the device's autonomous
resume). `RainGate` carries an extended non-work end (`hold_until`) that the
gate service pushes to the device, and collapses it back to the user target
once the grass is dry.

It is evaluated near the boundary only (within `GATE_WINDOW` before the current
device end) so the extension is asserted just-in-time — one write per dry-out
period, not every tick. While it is actively raining the extension is
provisional (`now + dry_hours`, since the previous rain-end on record is stale);
once the rain stops `dry_at` (rain_ended + dry_hours) pins it precisely.

State is in-memory (not persisted): a mid-hold HA restart forgets `hold_until`,
leaving the device on the extended window until the next dry boundary. That is
surfaced by `binary_sensor.luba_non_work_drift` (un-muted once the gate is no
longer holding) — the user restores via the manual push button.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import ClassVar


@dataclass
class RainGate:
    """Owns the rain-extended non-work end (in-memory; mutator-returns-bool)."""

    GATE_WINDOW: ClassVar[timedelta] = timedelta(minutes=10)  # closeness to act

    hold_until: datetime | None = None

    @property
    def is_holding(self) -> bool:
        """True while we are extending non-work past the user target."""
        return self.hold_until is not None

    def evaluate(
        self,
        now: datetime,
        target_end: datetime | None,
        currently_wet: bool,
        dry_at: datetime | None,
        dry_hours: float,
    ) -> bool:
        """Recompute the hold near the boundary. Returns True if it changed.

        `target_end` is the upcoming user-target morning end as a datetime
        (`None` when we are not inside the quiet window — see
        `NonWorkHours.end_of_active_window`). The device's real end is
        `hold_until` while holding, else `target_end`; we only act within
        `GATE_WINDOW` before it.
        """
        device_end = self.hold_until or target_end
        if device_end is None:
            return self._clear()  # outside the window and not holding — idle
        if device_end - now > self.GATE_WINDOW:
            return False  # not near the boundary yet — leave the hold untouched
        if self._is_dry(now, currently_wet, dry_at):
            return self._clear()  # grass dry — restore target, allow auto-resume
        floor = now + timedelta(hours=dry_hours) if currently_wet else dry_at
        base = target_end or device_end
        return self._set_hold(max(base, floor) if floor is not None else base)

    @staticmethod
    def _is_dry(now: datetime, currently_wet: bool, dry_at: datetime | None) -> bool:
        return not currently_wet and (dry_at is None or dry_at <= now)

    def _set_hold(self, value: datetime) -> bool:
        if self.hold_until == value:
            return False
        self.hold_until = value
        return True

    def _clear(self) -> bool:
        if self.hold_until is None:
            return False
        self.hold_until = None
        return True
