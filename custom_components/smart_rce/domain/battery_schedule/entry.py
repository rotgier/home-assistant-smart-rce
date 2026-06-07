"""Slot domain entities and supporting types.

Contains `BatteryScheduleEntry` (main) + `SlotKind` / `SlotProfile`
+ `SlotBehavior` / `NotificationLevel` + `DisengageReason`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum, StrEnum
from typing import Any

from ..ems_operation import EmsOperation
from .direction import Direction
from .operation import BatteryOperation


class SlotBehavior(StrEnum):
    """When inside `[start, end)` window, when to actually engage EMS."""

    IMMEDIATE = "IMMEDIATE"
    """Start ASAP at `start`. Stops on target_soc or end."""

    DELAYED_TO_END = "DELAYED_TO_END"
    """Delay engagement so target_soc is reached just before `end`. Default."""


class NotificationLevel(StrEnum):
    """Telegram notification urgency.

    NORMAL → telegram + persistent notification (reuse existing
             `script.notify_alert` with voice variable = False).
    EMERGENCY → adds voice call. OK during evening discharge (user is awake),
                NOT for morning slots (would wake them up).
    """

    NORMAL = "NORMAL"
    EMERGENCY = "EMERGENCY"


class DisengageReason(StrEnum):
    """Why a scheduled slot stopped engaging — used by `SlotDisengaged` event.

    None (from `Entry.disengage_reason`) = keep engaging.
    """

    TARGET_REACHED = "target_reached"
    """SoC reached target (normal completion)."""

    WINDOW_ENDED = "window_ended"
    """`now` moved past `end` (window timeout)."""

    DISABLED = "disabled"
    """`slot.enabled` flipped to False mid-engagement."""


@dataclass(frozen=True)
class BatteryScheduleEntry:
    """Single time-windowed battery operation slot. Immutable value object.

    `kind` is structural (tied to slot position in the aggregate). User edits
    the other four fields via UI.

    Validation (raises `ValueError`):
    - `0 <= target_soc <= 100`
    - `start < end` when `enabled=True`
    """

    kind: SlotKind
    enabled: bool = False
    start: time = time(0, 0)
    end: time = time(0, 0)
    target_soc: float = 10.0
    behavior: SlotBehavior = SlotBehavior.DELAYED_TO_END

    def __post_init__(self) -> None:
        if not 0.0 <= self.target_soc <= 100.0:
            raise ValueError(f"target_soc {self.target_soc} outside [0, 100]")
        if self.enabled and self.start >= self.end:
            raise ValueError(
                f"start {self.start} must be before end {self.end} when enabled"
            )

    # ─── Factories ───

    @classmethod
    def defaults_for_all_kinds(cls) -> dict[SlotKind, BatteryScheduleEntry]:
        """Disabled-default entry for every `SlotKind` — used by aggregate factory."""
        return {k: cls.default_for(k) for k in SlotKind}

    @classmethod
    def default_for(
        cls, kind: SlotKind, *, enabled: bool = False
    ) -> BatteryScheduleEntry:
        start, end = kind.profile.default_window
        return cls(
            kind=kind,
            enabled=enabled,
            start=start,
            end=end,
            target_soc=kind.profile.default_target_soc,
        )

    # ─── with_* mutators (immutable replace) ───

    def with_enabled(self, value: bool) -> BatteryScheduleEntry:
        return dataclasses.replace(self, enabled=value)

    def with_start(self, value: time) -> BatteryScheduleEntry:
        return dataclasses.replace(self, start=value)

    def with_end(self, value: time) -> BatteryScheduleEntry:
        return dataclasses.replace(self, end=value)

    def with_target_soc(self, value: float) -> BatteryScheduleEntry:
        return dataclasses.replace(self, target_soc=value)

    def with_behavior(self, value: SlotBehavior) -> BatteryScheduleEntry:
        return dataclasses.replace(self, behavior=value)

    # ─── Predicates (window + target + lifecycle) ───

    def should_apply_now(self, now: datetime, current_soc: float) -> bool:
        """Whether orchestrator should actively engage EMS mode at `now`.

        Returns False if:
        - slot is disabled
        - `now` outside `[start, end)`
        - `target_soc` already reached
        - `behavior=DELAYED_TO_END` and remaining window time still exceeds
          the projected time-to-complete

        `behavior=IMMEDIATE` → True as soon as inside window with target not
        reached.

        NOTE: orchestrator applies hysteresis on top — once engaged, sticks
        until target_reached or out of window, even if `should_apply_now`
        flickers (e.g. SoC drops faster than expected). See
        `BatterySchedule.compute_operation`.
        """
        if not self.enabled:
            return False
        if not self.is_in_window(now):
            return False
        if self.soc_target_reached(current_soc):
            return False
        if self.behavior == SlotBehavior.IMMEDIATE:
            return True
        # DELAYED_TO_END: engage only when remaining window time is just
        # enough to hit target at the assumed rate.
        return self._sec_until_end(now) <= self.time_to_complete_at(current_soc)

    def _sec_until_end(self, now: datetime) -> float:
        """Seconds from `now` until today's `end` time. Negative if already past."""
        end_dt = datetime.combine(now.date(), self.end, tzinfo=now.tzinfo)
        return (end_dt - now).total_seconds()

    def time_to_complete_at(self, current_soc: float) -> float:
        """Seconds needed to reach target_soc via zone-aware rate model.

        Delegates to `direction.seconds_for_soc_traversal` — direction-agnostic
        since it normalizes start/end internally. Returns 0 if already at target.

        Zone-aware vs constant 75 sec/pp matters for full-depth discharges
        (100→10%): empirical 104 min vs constant-model 112.5 min — DELAYED
        engagement starts ~8 min later, less time at extreme SoC.
        """
        if self.soc_target_reached(current_soc):
            return 0.0
        return self.kind.direction.seconds_for_soc_traversal(
            current_soc, self.target_soc
        )

    def disengage_reason(self, now: datetime, soc: float) -> DisengageReason | None:
        """Return None if entry should keep engaging; otherwise the reason to stop.

        Used by `BatterySchedule.compute_operation` to decide whether to hold a
        currently-engaging slot (None → stay) or release it (reason → emit
        SlotDisengaged event with the same reason).
        """
        if not self.enabled:
            return DisengageReason.DISABLED
        if not self.is_in_window(now):
            return DisengageReason.WINDOW_ENDED
        if self.soc_target_reached(soc):
            return DisengageReason.TARGET_REACHED
        return None

    def is_in_window(self, now: datetime) -> bool:
        """Return True if `now` falls inside `[start, end)`. Ignores `enabled`."""
        return self.start <= now.time() < self.end

    def soc_target_reached(self, current_soc: float) -> bool:
        """Return True when no further work needed for this direction.

        Discharge → SoC <= target. Charge → SoC >= target.
        """
        if self.kind.direction.is_discharge:
            return current_soc <= self.target_soc
        return current_soc >= self.target_soc

    # ─── Output + serialization ───

    def to_battery_operation(self) -> BatteryOperation:
        """Build BatteryOperation (output) from this slot entry.

        Caller (aggregate `compute_operation` / `current_operation`) uses this
        to translate engaged slot → ems_op + needs_charge_toggle without
        BatteryOperation having to know Entry's internals.
        """
        d = self.kind.direction
        return BatteryOperation(
            ems_op=EmsOperation(
                ems_mode=d.ems_mode,
                power_limit_w=d.power_limit_w,
                source="schedule",
                reason=f"slot={self.kind.name}",
            ),
            needs_charge_toggle=d.needs_charge_toggle,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "start": self.start.isoformat(timespec="minutes"),
            "end": self.end.isoformat(timespec="minutes"),
            "target_soc": self.target_soc,
            "behavior": self.behavior.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, kind: SlotKind) -> BatteryScheduleEntry:
        defaults = kind.profile
        start, end = defaults.default_window
        return cls(
            kind=kind,
            enabled=bool(data.get("enabled", False)),
            start=time.fromisoformat(
                data.get("start", start.isoformat(timespec="minutes"))
            ),
            end=time.fromisoformat(data.get("end", end.isoformat(timespec="minutes"))),
            target_soc=float(data.get("target_soc", defaults.default_target_soc)),
            behavior=SlotBehavior(
                data.get("behavior", SlotBehavior.DELAYED_TO_END.value)
            ),
        )


@dataclass(frozen=True)
class SlotProfile:
    """Per-kind metadata: direction (shared) + slot-specific defaults & policy.

    Rate (sec/pp) lives on `direction.rate_zones` — per-direction zones
    cover non-linear inverter behavior across SoC range. No per-kind rate
    override (all DISCHARGE slots share same zones).
    """

    direction: Direction
    notification_level: NotificationLevel
    default_window: tuple[time, time]
    default_target_soc: float


class SlotKind(Enum):
    """Battery schedule slot kinds. Each value is its `SlotProfile`.

    Precedence (last wins) — see `by_precedence()` classmethod. Declaration order
    here is alphabetical-by-category and does NOT equal precedence.
    """

    CHARGE_MORNING = SlotProfile(
        direction=Direction.CHARGE,
        notification_level=NotificationLevel.NORMAL,
        default_window=(time(2, 0), time(6, 0)),
        default_target_soc=100.0,
    )

    DISCHARGE_MORNING = SlotProfile(
        direction=Direction.DISCHARGE,
        notification_level=NotificationLevel.NORMAL,
        # NO voice call — would wake user up.
        default_window=(time(6, 0), time(9, 0)),
        default_target_soc=10.0,
    )

    CHARGE_AFTERNOON = SlotProfile(
        direction=Direction.CHARGE,
        notification_level=NotificationLevel.NORMAL,
        default_window=(time(13, 0), time(19, 0)),
        # April-September. Other months user shortens to (13, 16).
        default_target_soc=80.0,
        # 80% leaves headroom for late-afternoon PV surplus.
    )

    DISCHARGE_EVENING = SlotProfile(
        direction=Direction.DISCHARGE,
        notification_level=NotificationLevel.EMERGENCY,
        # Voice call OK — user is awake during evening peak.
        default_window=(time(20, 0), time(22, 0)),
        default_target_soc=10.0,
    )

    @property
    def profile(self) -> SlotProfile:
        return self.value

    @property
    def direction(self) -> Direction:
        return self.value.direction

    @classmethod
    def by_precedence(cls) -> list[SlotKind]:
        """Precedence order — last wins when multiple slots in window.

        Rules: Discharge beats charge (RCE peaks are time-critical; charging
        can wait). Evening discharge beats morning (typically higher RCE
        peak). Afternoon charge beats morning charge (closer to use, less
        time wasted holding).
        """
        return [
            cls.CHARGE_MORNING,
            cls.CHARGE_AFTERNOON,
            cls.DISCHARGE_MORNING,
            cls.DISCHARGE_EVENING,
        ]
