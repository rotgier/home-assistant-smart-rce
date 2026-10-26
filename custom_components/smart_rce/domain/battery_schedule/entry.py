"""Slot domain entities and supporting types.

Contains `BatteryScheduleEntry` (main) + `SlotKind` / `SlotProfile`
+ `SlotBehavior` / `NotificationLevel` + `DisengageReason`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum, StrEnum
from typing import Any, Final

from ..ems_operation import EmsOperation
from .direction import Direction, RateZone
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
    EMERGENCY → adds voice call. Only the workday morning auto discharge
                uses it; never for slots at dawn (would wake the user up).
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

    def with_plan(
        self, start: time, end: time, target_soc: float
    ) -> BatteryScheduleEntry:
        """Window and target from a plan, landing on the target at the end."""
        return dataclasses.replace(
            self,
            start=start,
            end=end,
            target_soc=target_soc,
            behavior=SlotBehavior.DELAYED_TO_END,
        )

    # ─── Predicates (window + target + lifecycle) ───

    def should_apply_now(self, now: datetime, current_soc: float) -> bool:
        """Whether orchestrator should actively engage EMS mode at `now`.

        Returns False if:
        - slot is disabled
        - `now` outside `[start, end)`
        - `target_soc` already reached
        - the gap to `target_soc` is below the kind's `min_start_gap_pp`
        - `behavior=DELAYED_TO_END` and remaining window time still exceeds
          the projected time-to-complete plus the kind's `delayed_lead_sec`

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
        if not self.needs_run_from(current_soc):
            return False
        if self.behavior == SlotBehavior.IMMEDIATE:
            return True
        # DELAYED_TO_END: engage only when remaining window time is just
        # enough to hit target at the assumed rate (plus the kind's lead, a
        # margin against the rate model running slow).
        return (
            self._sec_until_end(now)
            <= self.time_to_complete_at(current_soc)
            + self.kind.profile.delayed_lead_sec
        )

    def needs_run_from(self, current_soc: float) -> bool:
        """Whether enough work is left from `current_soc` to start a run."""
        return (
            not self.soc_target_reached(current_soc)
            and self._gap_to_target(current_soc) >= self.kind.profile.min_start_gap_pp
        )

    def _gap_to_target(self, current_soc: float) -> float:
        """Percentage points of work left — positive while target not reached."""
        if self.kind.direction.is_discharge:
            return current_soc - self.target_soc
        return self.target_soc - current_soc

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
        return self.kind.profile.seconds_for_soc_traversal(current_soc, self.target_soc)

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
    cover non-linear inverter behavior across SoC range. A kind may override
    them with its own `rate_zones` when its sessions behave differently
    (the morning auto discharge, see `_MORNING_DISCHARGE_ZONES`).
    """

    direction: Direction
    notification_level: NotificationLevel
    default_window: tuple[time, time]
    default_target_soc: float
    # Target comes from the morning-charge forecast instead of the slot's own
    # number, which then serves only as the fallback when no forecast exists.
    auto_target: bool = False
    # Window and target come from today's `MorningDischargePlan`; without a plan
    # the slot stands still. The slot's own window bounds the hours searched.
    planned: bool = False
    # Stays out on a day the workday sensor reports as off.
    workdays_only: bool = False
    # Level on a day off, when it differs from `notification_level` (None:
    # the same every day).
    day_off_notification_level: NotificationLevel | None = None
    # DELAYED_TO_END starts this many seconds earlier than the rate model asks.
    # Charge slots: a charge that lands a few minutes early costs nothing, one
    # that misses its deadline buys the shortfall at the next zone's price.
    # The planned morning discharge too: its target moves during the hour, and
    # a run ending at the hour's last second has no room left for a lower one.
    delayed_lead_sec: float = 0.0
    # Engage only when at least this many pp of work are left — keeps a
    # forecast jittering by a point or two from starting a one-minute charge.
    min_start_gap_pp: float = 0.0
    # Own SoC-zone rate model; None = the direction's.
    rate_zones: tuple[RateZone, ...] | None = None

    def runs_on(self, day: date, is_workday: bool | None) -> bool:
        """Whether the kind takes part on `day` (unknown calendar = workday)."""
        return not (self.workdays_only and is_day_off(day, is_workday))

    def notification_level_on(self, day_off: bool) -> NotificationLevel:
        """How loudly a session of this kind is announced on such a day."""
        if day_off and self.day_off_notification_level is not None:
            return self.day_off_notification_level
        return self.notification_level

    def seconds_for_soc_traversal(self, start_soc: float, end_soc: float) -> float:
        """Seconds to move SoC start→end at this kind's rate model."""
        if self.rate_zones is None:
            return self.direction.seconds_for_soc_traversal(start_soc, end_soc)
        low, high = min(start_soc, end_soc), max(start_soc, end_soc)
        if low >= high:
            return 0.0
        return sum(z.overlap_seconds(low, high) for z in self.rate_zones)


# Lead for a DELAYED_TO_END start: 4 min ~ 3 pp of charge, or of discharge
# above 25% (75 s/pp; below 25% ~6 pp). Every CHARGE slot and the morning
# auto discharge (user's decision 16.10.2026 — that morning the 07 hour ended
# at 07:59:55: the 30 <-> 31 target wobble had no room; 5 min would only add
# ~0.8 pp and leave the battery on target longer while the target may rise).
_DELAYED_LEAD_SEC = 240.0

# Morning discharge pace — after the night top-up the BMS reports SoC
# differently than after a full day charge (evening: below ~25% twice as fast,
# which `Direction.DISCHARGE` models). Every morning session 07-16.10.2026
# (07, 08, 09, 16.10), at a steady ~5.1 kW: ~72 s/pp down to 21%, then 21%
# holds for 454-537 s, then 21->19 in ~10 s, below ~40-76 s/pp. Rounded up, as
# for charging: a run that starts early still finishes, one that starts late
# does not (16.10 08:40: the evening model expected 26->19 in ~5 min, it took
# ~14). home-assistant-ops research/2026-10-17-tempo-rozladowania-rano/.
_MORNING_DISCHARGE_ZONES: Final[tuple[RateZone, ...]] = (
    RateZone(soc_from=21.0, soc_to=100.01, sec_per_pp=75.0),
    RateZone(soc_from=20.0, soc_to=21.0, sec_per_pp=540.0),
    RateZone(soc_from=19.0, soc_to=20.0, sec_per_pp=10.0),
    RateZone(soc_from=0.0, soc_to=19.0, sec_per_pp=60.0),
)


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
        delayed_lead_sec=_DELAYED_LEAD_SEC,
    )

    CHARGE_MORNING_AUTO = SlotProfile(
        direction=Direction.CHARGE,
        notification_level=NotificationLevel.NORMAL,
        # Workdays only (gated in `BatterySchedule`): 07:00 opens T1. A full
        # 10->100% charge takes ~2h48m, so 04:00 always leaves enough room.
        default_window=(time(4, 0), time(7, 0)),
        # Fallback only — used when neither a fresh nor a remembered forecast
        # target exists for the day.
        default_target_soc=60.0,
        auto_target=True,
        workdays_only=True,
        delayed_lead_sec=_DELAYED_LEAD_SEC,
        min_start_gap_pp=3.0,
    )

    DISCHARGE_MORNING_AUTO = SlotProfile(
        direction=Direction.DISCHARGE,
        # Voice call on purpose (user's wish 15.10.2026): an automatic sale of
        # the battery is worth hearing about, and 07-S0 nobody is asleep.
        notification_level=NotificationLevel.EMERGENCY,
        # Every day since 17.10.2026 (it took over the "Sunday Morning
        # Discharge to 10%" automation). A day off searches from 05:00, so
        # text only — no call at dawn (user's decision 17.10.2026).
        day_off_notification_level=NotificationLevel.NORMAL,
        # The hours searched for the dearest one (a day off from 05:00, see
        # `MorningDischargePlan`); the plan narrows the window to that hour
        # and also stops it at the charge start.
        default_window=(time(7, 0), time(13, 0)),
        # Never discharges by itself: without a plan the slot stands still.
        default_target_soc=100.0,
        planned=True,
        # A target moving by a point must not start a one-minute discharge.
        min_start_gap_pp=2.0,
        delayed_lead_sec=_DELAYED_LEAD_SEC,
        rate_zones=_MORNING_DISCHARGE_ZONES,
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
        # Ends where the summer evening peak begins. Winter T2 starts at 16:00,
        # so the window wants shortening then — but only the user can do that:
        # this value seeds a fresh install, and a persisted window is never
        # rewritten from here.
        default_window=(time(13, 0), time(19, 0)),
        default_target_soc=80.0,
        delayed_lead_sec=_DELAYED_LEAD_SEC,
        # 80% leaves headroom for late-afternoon PV surplus.
        # Workdays only (user's decision 17.10.2026): it fills the battery for
        # the evening T2, and a day off has no T2 — the whole day is T3.
        workdays_only=True,
    )

    DISCHARGE_EVENING_XXEARLY = SlotProfile(
        direction=Direction.DISCHARGE,
        notification_level=NotificationLevel.NORMAL,
        # Winter's first T2 hour (16:00), a rung of `EveningPlan` when it is
        # worth selling in: it stands on 75%, the reserve the four T2 hours
        # after it need (user's decision 23.10.2026).
        default_window=(time(16, 0), time(17, 0)),
        default_target_soc=75.0,
    )

    DISCHARGE_EVENING_XEARLY = SlotProfile(
        direction=Direction.DISCHARGE,
        notification_level=NotificationLevel.NORMAL,
        # Winter's 17:00 rung, standing on 61% for the three T2 hours after it.
        default_window=(time(17, 0), time(18, 0)),
        default_target_soc=61.0,
    )

    DISCHARGE_EVENING_EARLY = SlotProfile(
        direction=Direction.DISCHARGE,
        # Text only (user's decision 26.10.2026): since the evening is planned
        # hour by hour, these slots start and stop several times an evening and
        # a call for each was noise.
        notification_level=NotificationLevel.NORMAL,
        default_window=(time(19, 0), time(21, 0)),
        default_target_soc=10.0,
    )

    DISCHARGE_EVENING_MID = SlotProfile(
        direction=Direction.DISCHARGE,
        notification_level=NotificationLevel.NORMAL,
        # The second-to-last T2 hour's rung (winter 19:00, 33%). Its job is
        # less to sell than to be standing at the reserve when the hour
        # closes — see `EveningPlan._rung_for`.
        default_window=(time(20, 0), time(21, 0)),
        default_target_soc=33.0,
    )

    DISCHARGE_EVENING_LATE = SlotProfile(
        direction=Direction.DISCHARGE,
        notification_level=NotificationLevel.NORMAL,
        # Second evening window. Nights where the RCE peak is followed by
        # another expensive hour need two discharges with a hold in between
        # (drain to ~33% in the peak, keep the rest for the later hour).
        # Before this slot existed that second window was improvised by
        # repointing DISCHARGE_MORNING at 21:00-22:00.
        default_window=(time(21, 0), time(22, 0)),
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
        time wasted holding). The manual morning charge beats the automatic
        one — it is the fallback the user reaches for when auto misbehaves; the
        same holds for the morning discharge.
        """
        return [
            cls.CHARGE_MORNING_AUTO,
            cls.CHARGE_MORNING,
            cls.CHARGE_AFTERNOON,
            cls.DISCHARGE_MORNING_AUTO,
            cls.DISCHARGE_MORNING,
            cls.DISCHARGE_EVENING_XXEARLY,
            cls.DISCHARGE_EVENING_XEARLY,
            cls.DISCHARGE_EVENING_EARLY,
            cls.DISCHARGE_EVENING_MID,
            cls.DISCHARGE_EVENING_LATE,
        ]


def is_day_off(day: date, is_workday: bool | None) -> bool:
    """Tell whether `day` is off by the workday sensor (unknown = workday)."""
    return is_workday is False
