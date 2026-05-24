"""Battery schedule — user/proposer intent for daily charge/discharge windows.

Etap 0 skeleton: aggregate structure + override property + persistence. The
behavior layer (matching schedule windows to current time, engaging slots,
emitting events, producing BatteryOperation) lives in Etap 2A — added in a
later iteration. For now the aggregate exists so that:

1. `BatterySchedule.ems_interventions_blocked` (derived from `_user_override`
   + `_currently_engaging`) replaces the legacy
   `input_boolean.ems_allow_discharge_override` as source of truth for
   DodPolicy and GridExportManager.
2. `BatteryScheduleRepository` persists this aggregate via own Store
   (immediate, SAVE_DELAY=0) — survives HA crash.
3. `SmartRceEmsOverrideSwitch` HA entity views/mutates `_user_override` via
   the repository.

Eight named slots — four per day (today / tomorrow). Slot semantics:

- CHARGE_MORNING      (cheap night/early-morning RCE hours, ~02-06)
- DISCHARGE_MORNING   (secondary morning peak; PV+battery hybrid via DISCHARGE_PV)
- CHARGE_AFTERNOON    (cheap afternoon RCE — pre-fill battery before evening
                       peak if PV is insufficient. April-September: 13-19;
                       other months: 13-16. User sets window manually per season.)
- DISCHARGE_EVENING   (primary RCE peak; voice-call notification OK)

Behavior model placeholder (full implementation Etap 2A):
- `SlotBehavior.IMMEDIATE`     — engage at `start`, stop at target_soc/end
- `SlotBehavior.DELAYED_TO_END` — delay start so target_soc is reached just
                                  before `end`. Matches the legacy
                                  `sec_to_end <= (soc - target) * rate` template.

Reload safety: `DISCHARGE` and `CHARGE` are module-level singletons of
`Direction`. After `live_reload()` they are re-created as new instances —
`is` comparison breaks. Use `direction.is_discharge` / `direction.name`
(string-based) for comparisons that must survive reload.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum, StrEnum
from typing import Any, Final, Literal

# ─────────────────────────────────────────────────────────────────────────────
# Input DTO — what the schedule needs from outside on each tick
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BatteryScheduleInput:
    """Subset of system input the schedule service needs each tick.

    Domain VO — no HA dependencies. Application layer (Ems body) translates
    the HA `InputState` snapshot to this via a one-line factory call.

    Etap 0: just SoC. Future extensions:
    - battery_power_w (dynamic rate adjustment per actual discharge rate)
    - any future signals service needs to make decisions
    """

    battery_soc: float | None


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────


class EmsMode(StrEnum):
    """Mirror of `select.goodwe_ems_mode` values we care about.

    Per ADR-017, new automations use EMS modes (sell_power/discharge_pv/
    charge_battery) instead of operation_mode (which clears EMS state).
    """

    AUTO = "auto"
    DISCHARGE_BATTERY = "discharge_battery"
    CHARGE_BATTERY = "charge_battery"
    SELL_POWER = "sell_power"
    DISCHARGE_PV = "discharge_pv"


class NotificationLevel(StrEnum):
    """Telegram notification urgency.

    NORMAL → telegram + persistent notification (reuse existing
             `script.notify_alert` with voice variable = False).
    EMERGENCY → adds voice call. OK during evening discharge (user is awake),
                NOT for morning slots (would wake them up).
    """

    NORMAL = "NORMAL"
    EMERGENCY = "EMERGENCY"


class SlotBehavior(StrEnum):
    """When inside `[start, end)` window, when to actually engage EMS."""

    IMMEDIATE = "IMMEDIATE"
    """Start ASAP at `start`. Stops on target_soc or end."""

    DELAYED_TO_END = "DELAYED_TO_END"
    """Delay engagement so target_soc is reached just before `end`. Default."""


# ─────────────────────────────────────────────────────────────────────────────
# Direction — singleton per battery flow direction
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Direction:
    """Battery flow direction (DISCHARGE / CHARGE) + per-direction settings.

    Two module-level singleton instances: `DISCHARGE` and `CHARGE`. Every
    `SlotKind` references one. Carries everything that's the same across
    all slots of that direction (EMS mode, power limit, charge toggle
    requirement). Per-slot variation lives in `SlotProfile`.

    Comparison: NEVER use `is` (breaks after `live_reload()`). Use
    `direction.is_discharge` / `direction.is_charge` properties or
    `direction.name == "DISCHARGE"` string compare.
    """

    name: Literal["DISCHARGE", "CHARGE"]
    ems_mode: EmsMode
    power_limit_w: int
    needs_charge_toggle: bool

    @property
    def is_discharge(self) -> bool:
        return self.name == "DISCHARGE"

    @property
    def is_charge(self) -> bool:
        return self.name == "CHARGE"


DISCHARGE: Final = Direction(
    name="DISCHARGE",
    ems_mode=EmsMode.DISCHARGE_PV,
    # PV+battery hybrid. Morning: PV covers load, battery supplies overflow.
    # Evening: PV is zero, mode degrades to battery-only — same effect as
    # DISCHARGE_BATTERY without needing a second EMS mode in the matrix.
    power_limit_w=6000,
    needs_charge_toggle=False,
)


CHARGE: Final = Direction(
    name="CHARGE",
    ems_mode=EmsMode.CHARGE_BATTERY,
    power_limit_w=6000,
    needs_charge_toggle=True,
    # `input_boolean.battery_charge_max_current_toggle` must be ON during
    # charge — BMS guard. Today set via service call; will be migrated to a
    # smart_rce-owned switch with continuous DodPolicy-like control in a
    # separate plan (Etap 3).
)


# Default inverter rate (seconds per 1% SoC change at the EMS power_limit).
# 75 s/% observed from the evening 6000W discharge automation. Symmetric
# estimate for charge until measured separately. Hardcoded — per-entry
# override is post-MVP.
DEFAULT_RATE_SEC_PER_PCT: Final[float] = 75.0


# ─────────────────────────────────────────────────────────────────────────────
# SlotProfile + SlotKind
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SlotProfile:
    """Per-kind metadata: direction (shared) + slot-specific defaults & policy."""

    direction: Direction
    notification_level: NotificationLevel
    default_window: tuple[time, time]
    default_target_soc: float
    rate_sec_per_pct: float = DEFAULT_RATE_SEC_PER_PCT


class SlotKind(Enum):
    """Battery schedule slot kinds. Each value is its `SlotProfile`."""

    CHARGE_MORNING = SlotProfile(
        direction=CHARGE,
        notification_level=NotificationLevel.NORMAL,
        default_window=(time(2, 0), time(6, 0)),
        default_target_soc=100.0,
    )

    DISCHARGE_MORNING = SlotProfile(
        direction=DISCHARGE,
        notification_level=NotificationLevel.NORMAL,
        # NO voice call — would wake user up.
        default_window=(time(6, 0), time(9, 0)),
        default_target_soc=10.0,
    )

    CHARGE_AFTERNOON = SlotProfile(
        direction=CHARGE,
        notification_level=NotificationLevel.NORMAL,
        default_window=(time(13, 0), time(19, 0)),
        # April-September. Other months user shortens to (13, 16).
        default_target_soc=80.0,
        # 80% leaves headroom for late-afternoon PV surplus.
    )

    DISCHARGE_EVENING = SlotProfile(
        direction=DISCHARGE,
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


# ─────────────────────────────────────────────────────────────────────────────
# BatteryScheduleEntry — single slot value object (immutable)
# ─────────────────────────────────────────────────────────────────────────────


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

    def with_changes(self, **kwargs: Any) -> BatteryScheduleEntry:
        return dataclasses.replace(self, **kwargs)

    # ─── window + target predicates ───

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

    def time_to_complete_at(self, current_soc: float) -> float:
        """Seconds needed to reach target_soc at the default inverter rate.

        Returns 0 if already at target. Symmetric for charge/discharge —
        absolute SoC delta × rate. Never negative.
        """
        if self.soc_target_reached(current_soc):
            return 0.0
        delta = abs(current_soc - self.target_soc)
        return delta * self.kind.profile.rate_sec_per_pct

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
        sec_to_end = _sec_until_today(now, self.end)
        return sec_to_end <= self.time_to_complete_at(current_soc)

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


# ─────────────────────────────────────────────────────────────────────────────
# BatterySchedule — aggregate root
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class BatterySchedule:
    """Aggregate root — 4 named slots × 2 days = 8 entries.

    Plus two state fields that drive `ems_interventions_blocked`:
    - `_currently_engaging` (persisted): which slot is currently being executed
      by the orchestrator (set by `compute_operation` — Etap 2A).
    - `_interventions_blocked_override` (persisted): user-driven manual override
      from `switch.ems_interventions_blocked`.

    The two combine via OR: any reason → interventions blocked → DodPolicy
    stays at DoD=90.

    Midnight roll: tomorrow_* → today_*, tomorrow_* reset to disabled defaults.
    Implemented in `roll_day()`; called by compute_operation in Etap 2A.
    """

    today_charge_morning: BatteryScheduleEntry = field(
        default_factory=lambda: BatteryScheduleEntry.default_for(
            SlotKind.CHARGE_MORNING
        )
    )
    today_discharge_morning: BatteryScheduleEntry = field(
        default_factory=lambda: BatteryScheduleEntry.default_for(
            SlotKind.DISCHARGE_MORNING
        )
    )
    today_charge_afternoon: BatteryScheduleEntry = field(
        default_factory=lambda: BatteryScheduleEntry.default_for(
            SlotKind.CHARGE_AFTERNOON
        )
    )
    today_discharge_evening: BatteryScheduleEntry = field(
        default_factory=lambda: BatteryScheduleEntry.default_for(
            SlotKind.DISCHARGE_EVENING
        )
    )
    tomorrow_charge_morning: BatteryScheduleEntry = field(
        default_factory=lambda: BatteryScheduleEntry.default_for(
            SlotKind.CHARGE_MORNING
        )
    )
    tomorrow_discharge_morning: BatteryScheduleEntry = field(
        default_factory=lambda: BatteryScheduleEntry.default_for(
            SlotKind.DISCHARGE_MORNING
        )
    )
    tomorrow_charge_afternoon: BatteryScheduleEntry = field(
        default_factory=lambda: BatteryScheduleEntry.default_for(
            SlotKind.CHARGE_AFTERNOON
        )
    )
    tomorrow_discharge_evening: BatteryScheduleEntry = field(
        default_factory=lambda: BatteryScheduleEntry.default_for(
            SlotKind.DISCHARGE_EVENING
        )
    )
    last_seen_date: date | None = None
    _currently_engaging: SlotKind | None = None
    _interventions_blocked_override: bool = False
    # When a slot disengages, this records the timestamp so consumers can ask
    # `is_active_this_hour(now)` — semantics matching legacy HA-side template
    # `binary_sensor.ems_other_automation_active_this_hour` that GridExportManager
    # uses to step aside in the clock hour following a smart_rce intervention.
    _last_disengaged_at: datetime | None = None

    @property
    def ems_interventions_blocked(self) -> bool:
        """True when smart_rce's internal interventions should step aside.

        Either user manually flipped the override (switch.ems_interventions_blocked)
        or orchestrator engaged a slot (charge/discharge active). Both reasons
        make DodPolicy stay at DoD=90.
        """
        return self._interventions_blocked_override or (
            self._currently_engaging is not None
        )

    def set_interventions_blocked_override(self, value: bool) -> bool:
        """Idempotent mutator — returns True if changed."""
        if self._interventions_blocked_override == value:
            return False
        self._interventions_blocked_override = value
        return True

    def is_active_this_hour(self, now: datetime) -> bool:
        """Return True if a slot is engaging OR disengaged within current clock hour.

        Replaces HA-side `binary_sensor.ems_other_automation_active_this_hour`
        signal (Etap C). Used by `GridExportManager.update` to step aside in
        the post-intervention cleanup window (rest of the clock hour after a
        smart_rce slot disengaged — avoids racing the inverter back to
        intervention state immediately after we cleaned up).
        """
        if self._currently_engaging is not None:
            return True
        if self._last_disengaged_at is None:
            return False
        return self._last_disengaged_at.replace(
            minute=0, second=0, microsecond=0
        ) == now.replace(minute=0, second=0, microsecond=0)

    def today_entries(self) -> dict[SlotKind, BatteryScheduleEntry]:
        return {
            SlotKind.CHARGE_MORNING: self.today_charge_morning,
            SlotKind.DISCHARGE_MORNING: self.today_discharge_morning,
            SlotKind.CHARGE_AFTERNOON: self.today_charge_afternoon,
            SlotKind.DISCHARGE_EVENING: self.today_discharge_evening,
        }

    def tomorrow_entries(self) -> dict[SlotKind, BatteryScheduleEntry]:
        return {
            SlotKind.CHARGE_MORNING: self.tomorrow_charge_morning,
            SlotKind.DISCHARGE_MORNING: self.tomorrow_discharge_morning,
            SlotKind.CHARGE_AFTERNOON: self.tomorrow_charge_afternoon,
            SlotKind.DISCHARGE_EVENING: self.tomorrow_discharge_evening,
        }

    def today_entry_for(self, kind: SlotKind) -> BatteryScheduleEntry:
        return self.today_entries()[kind]

    # ─── compute_operation — pure decision function w/ aggregate state mutations ───

    def compute_operation(
        self, now: datetime, current_soc: float
    ) -> tuple[BatteryOperation, list[BatteryScheduleEvent]]:
        """Decide BatteryOperation + emit domain events for what changed.

        Mutates aggregate state:
        - `last_seen_date` set to `now.date()` (rolls tomorrow→today on date change)
        - `_currently_engaging` flipped on engage/disengage

        Hysteresis: once `_currently_engaging` is set, keep engaging that slot
        until target_reached OR out-of-window OR slot disabled. Prevents
        flicker when SoC change rate diverges from the rate estimate.

        Precedence (when no current engagement, multiple slots in window):
        DISCHARGE_EVENING > DISCHARGE_MORNING > CHARGE_AFTERNOON > CHARGE_MORNING
        (`_PRECEDENCE` list — last wins as strongest).
        """
        events: list[BatteryScheduleEvent] = []

        # 1. Day roll detection
        if self.last_seen_date is not None and self.last_seen_date != now.date():
            events.append(DayRolled(from_date=self.last_seen_date, to_date=now.date()))
            self.roll_day()
        self.last_seen_date = now.date()

        # 2. Already engaging? Stick until target_reached / window_ended / disabled.
        if self._currently_engaging is not None:
            entry = self.today_entry_for(self._currently_engaging)
            disengage_reason = _disengage_reason(entry, now, current_soc)
            if disengage_reason is None:
                # Stay engaged — sticky hysteresis trumps DELAYED_TO_END flicker.
                return BatteryOperation.from_entry(entry), events
            events.append(
                SlotDisengaged(
                    slot=self._currently_engaging,
                    soc=current_soc,
                    at=now,
                    reason=disengage_reason,
                )
            )
            self._currently_engaging = None
            self._last_disengaged_at = now
            # Fall through — another slot might be ready to engage immediately.

        # 3. Find highest-precedence slot that should engage NOW.
        entry = self._find_engaging_entry(now, current_soc)
        if entry is not None:
            events.append(SlotEngaged(slot=entry.kind, soc=current_soc, at=now))
            self._currently_engaging = entry.kind
            return BatteryOperation.from_entry(entry), events

        return BatteryOperation.idle(), events

    def _find_engaging_entry(
        self, now: datetime, current_soc: float
    ) -> BatteryScheduleEntry | None:
        today = self.today_entries()
        engaging = [
            today[k] for k in _PRECEDENCE if today[k].should_apply_now(now, current_soc)
        ]
        return engaging[-1] if engaging else None

    def roll_day(self) -> None:
        """Shift tomorrow_* → today_*, reset tomorrow_* to disabled defaults.

        Idempotent — orchestrator should compare `last_seen_date` and call
        once per midnight crossing (Etap 2A).
        """
        self.today_charge_morning = self.tomorrow_charge_morning
        self.today_discharge_morning = self.tomorrow_discharge_morning
        self.today_charge_afternoon = self.tomorrow_charge_afternoon
        self.today_discharge_evening = self.tomorrow_discharge_evening
        self.tomorrow_charge_morning = BatteryScheduleEntry.default_for(
            SlotKind.CHARGE_MORNING
        )
        self.tomorrow_discharge_morning = BatteryScheduleEntry.default_for(
            SlotKind.DISCHARGE_MORNING
        )
        self.tomorrow_charge_afternoon = BatteryScheduleEntry.default_for(
            SlotKind.CHARGE_AFTERNOON
        )
        self.tomorrow_discharge_evening = BatteryScheduleEntry.default_for(
            SlotKind.DISCHARGE_EVENING
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "today": {k.name: e.to_dict() for k, e in self.today_entries().items()},
            "tomorrow": {
                k.name: e.to_dict() for k, e in self.tomorrow_entries().items()
            },
            "last_seen_date": (
                self.last_seen_date.isoformat() if self.last_seen_date else None
            ),
            "currently_engaging": (
                self._currently_engaging.name if self._currently_engaging else None
            ),
            "interventions_blocked_override": self._interventions_blocked_override,
            "last_disengaged_at": (
                self._last_disengaged_at.isoformat()
                if self._last_disengaged_at is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatterySchedule:
        def _entry(scope: str, kind: SlotKind) -> BatteryScheduleEntry:
            payload = (data.get(scope) or {}).get(kind.name)
            if payload is None:
                return BatteryScheduleEntry.default_for(kind)
            return BatteryScheduleEntry.from_dict(payload, kind=kind)

        last_seen_date: date | None = None
        if raw := data.get("last_seen_date"):
            try:
                last_seen_date = date.fromisoformat(raw)
            except ValueError:
                last_seen_date = None

        currently_engaging: SlotKind | None = None
        if engaging_name := data.get("currently_engaging"):
            try:
                currently_engaging = SlotKind[engaging_name]
            except KeyError:
                currently_engaging = None

        last_disengaged_at: datetime | None = None
        if raw := data.get("last_disengaged_at"):
            try:
                last_disengaged_at = datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                last_disengaged_at = None

        return cls(
            today_charge_morning=_entry("today", SlotKind.CHARGE_MORNING),
            today_discharge_morning=_entry("today", SlotKind.DISCHARGE_MORNING),
            today_charge_afternoon=_entry("today", SlotKind.CHARGE_AFTERNOON),
            today_discharge_evening=_entry("today", SlotKind.DISCHARGE_EVENING),
            tomorrow_charge_morning=_entry("tomorrow", SlotKind.CHARGE_MORNING),
            tomorrow_discharge_morning=_entry("tomorrow", SlotKind.DISCHARGE_MORNING),
            tomorrow_charge_afternoon=_entry("tomorrow", SlotKind.CHARGE_AFTERNOON),
            tomorrow_discharge_evening=_entry("tomorrow", SlotKind.DISCHARGE_EVENING),
            last_seen_date=last_seen_date,
            _currently_engaging=currently_engaging,
            _interventions_blocked_override=bool(
                data.get("interventions_blocked_override", False)
            ),
            _last_disengaged_at=last_disengaged_at,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Precedence + helpers (used by compute_operation)
# ─────────────────────────────────────────────────────────────────────────────


# Order for resolving overlapping `should_apply_now`. Last wins (= strongest).
# Discharge always beats charge (RCE peaks are time-critical; charging can wait).
# Evening discharge beats morning (typically higher RCE peak). Afternoon charge
# beats morning charge (closer to use, less time wasted holding).
_PRECEDENCE: Final[list[SlotKind]] = [
    SlotKind.CHARGE_MORNING,
    SlotKind.CHARGE_AFTERNOON,
    SlotKind.DISCHARGE_MORNING,
    SlotKind.DISCHARGE_EVENING,
]


# Reason for disengaging a currently-engaging slot. None = keep engaging.
DisengageReason = Literal["target_reached", "window_ended", "disabled"]


def _disengage_reason(
    entry: BatteryScheduleEntry, now: datetime, soc: float
) -> DisengageReason | None:
    """Return None if entry should keep engaging; otherwise the reason to stop."""
    if not entry.enabled:
        return "disabled"
    if not entry.is_in_window(now):
        return "window_ended"
    if entry.soc_target_reached(soc):
        return "target_reached"
    return None


def _sec_until_today(now: datetime, end: time) -> float:
    """Seconds from `now` until today's `end` time. Negative if already past."""
    end_dt = datetime.combine(now.date(), end, tzinfo=now.tzinfo)
    return (end_dt - now).total_seconds()


# ─────────────────────────────────────────────────────────────────────────────
# BatteryOperation — desired action this tick (input to Applier in Etap 2D)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BatteryOperation:
    """Desired battery operation at this moment.

    Output of `BatterySchedule.compute_operation`. Consumed by
    `BatteryOperationApplier` (Etap 2D, infrastructure) which translates fields
    into HA service calls. Equality is structural — service compares vs
    `_last_op` to skip no-op applies.

    NO `ems_override_active` field — `schedule.ems_interventions_blocked` is
    the canonical source of truth (read by Ems body and passed explicitly to
    DodPolicy/GridExportManager). NO `dod_force` — DodPolicy reacts via
    `INTERVENTIONS_BLOCKED` phase.

    `slot` carries the SlotKind responsible for this op (or None for idle).
    Used by Applier/Notifier to dispatch by kind without parsing strings.
    """

    ems_mode: EmsMode
    power_limit_w: int | None
    needs_charge_toggle: bool
    notification_level: NotificationLevel
    slot: SlotKind | None

    @property
    def is_idle(self) -> bool:
        return self.slot is None

    @classmethod
    def idle(cls) -> BatteryOperation:
        return cls(
            ems_mode=EmsMode.AUTO,
            power_limit_w=None,
            needs_charge_toggle=False,
            notification_level=NotificationLevel.NORMAL,
            slot=None,
        )

    @classmethod
    def from_entry(cls, entry: BatteryScheduleEntry) -> BatteryOperation:
        d = entry.kind.direction
        return cls(
            ems_mode=d.ems_mode,
            power_limit_w=d.power_limit_w,
            needs_charge_toggle=d.needs_charge_toggle,
            notification_level=entry.kind.profile.notification_level,
            slot=entry.kind,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Events — domain happenings emitted by compute_operation
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BatteryScheduleEvent:
    """Base marker class for domain events emitted by compute_operation.

    Notifier (Etap 2D) dispatches by isinstance — separate handler per type.
    """


@dataclass(frozen=True)
class SlotEngaged(BatteryScheduleEvent):
    """A slot just became active — orchestrator started engaging it."""

    slot: SlotKind
    soc: float
    at: datetime


@dataclass(frozen=True)
class SlotDisengaged(BatteryScheduleEvent):
    """A slot just stopped being active.

    `reason` distinguishes why:
    - "target_reached" — SoC reached target (normal completion)
    - "window_ended" — `now` moved past `end` (window timeout)
    - "disabled" — slot.enabled flipped to False mid-engagement
    """

    slot: SlotKind
    soc: float
    at: datetime
    reason: DisengageReason


@dataclass(frozen=True)
class DayRolled(BatteryScheduleEvent):
    """Midnight crossing detected — tomorrow_* shifted to today_*."""

    from_date: date
    to_date: date
