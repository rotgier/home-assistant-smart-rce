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
from datetime import date, datetime, time, timedelta
from enum import Enum, StrEnum
from typing import Any, Final, Literal, Protocol

from .ems_operation import EmsMode, EmsOperation

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
class RateZone:
    """SoC range with associated rate (seconds to traverse 1 pp).

    Zone covers `[soc_from, soc_to)` (half-open — `soc_to` exclusive).
    Used to model non-linear inverter behavior: BMS-compressed mid-range
    (25→16% discharges fast as pp represent less energy), calibration
    pauses (16→14%), and post-calibration fast end (14→10%). See
    `research/2026-05-04-battery-discharge-per-pp.csv` for empirical
    data.
    """

    soc_from: float
    soc_to: float
    sec_per_pp: float


# DISCHARGE rate zones — empirical from 2026-05-04 morning discharge session
# (DISCHARGE_BATTERY @ 6kW, BMS ~5kW effective). FAST ZONE covers 25-100
# (covers normal evening discharge 100→30); below 25 we hit BMS quirks.
DISCHARGE_RATE_ZONES: Final[tuple[RateZone, ...]] = (
    # FAST ZONE — typical discharge, ~75 sec/pp consistent
    RateZone(soc_from=25.0, soc_to=100.01, sec_per_pp=75.0),
    # BMS-COMP — middle range compresses pp (BMS lookup table), fast
    RateZone(soc_from=16.0, soc_to=25.0, sec_per_pp=36.0),
    # ANOMALY — calibration pause window
    RateZone(soc_from=14.0, soc_to=16.0, sec_per_pp=97.0),
    # FAST END — after calibration, fast again
    RateZone(soc_from=0.0, soc_to=14.0, sec_per_pp=34.0),
)

# CHARGE rate zones — no empirical data yet, uniform 75 sec/pp stub.
# TODO: collect empirical data + replace with zones analogous to discharge.
CHARGE_RATE_ZONES: Final[tuple[RateZone, ...]] = (
    RateZone(soc_from=0.0, soc_to=100.01, sec_per_pp=75.0),
)


@dataclass(frozen=True)
class Direction:
    """Battery flow direction (DISCHARGE / CHARGE) + per-direction settings.

    Two module-level singleton instances: `DISCHARGE` and `CHARGE`. Every
    `SlotKind` references one. Carries everything that's the same across
    all slots of that direction (EMS mode, power limit, charge toggle
    requirement, rate zones). Per-slot variation lives in `SlotProfile`.

    Comparison: NEVER use `is` (breaks after `live_reload()`). Use
    `direction.is_discharge` / `direction.is_charge` properties or
    `direction.name == "DISCHARGE"` string compare.
    """

    name: Literal["DISCHARGE", "CHARGE"]
    ems_mode: EmsMode
    power_limit_w: int
    needs_charge_toggle: bool
    rate_zones: tuple[RateZone, ...]

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
    rate_zones=DISCHARGE_RATE_ZONES,
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
    rate_zones=CHARGE_RATE_ZONES,
)


def seconds_for_range(
    low_soc: float, high_soc: float, zones: tuple[RateZone, ...]
) -> float:
    """Sum sec_per_pp across zones covering the [low_soc, high_soc] range.

    Pure function — directional sense lives in caller (discharge passes
    target as low, current as high; charge swaps). Returns 0 when range
    inverted or empty. SoC outside zone coverage contributes 0 (caller's
    responsibility — zones should fully cover the relevant SoC domain).
    """
    if low_soc >= high_soc:
        return 0.0
    total = 0.0
    for zone in zones:
        overlap_low = max(low_soc, zone.soc_from)
        overlap_high = min(high_soc, zone.soc_to)
        if overlap_high > overlap_low:
            total += (overlap_high - overlap_low) * zone.sec_per_pp
    return total


# ─────────────────────────────────────────────────────────────────────────────
# SlotProfile + SlotKind
# ─────────────────────────────────────────────────────────────────────────────


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
        """Seconds needed to reach target_soc via zone-aware rate model.

        Sums sec_per_pp across `direction.rate_zones` for the [low, high]
        range. For DISCHARGE: range = [target_soc, current_soc]. For CHARGE:
        range = [current_soc, target_soc]. Returns 0 if already at target.

        Zone-aware vs constant 75 sec/pp matters for full-depth discharges
        (100→10%): empirical 104 min vs constant-model 112.5 min — DELAYED
        engagement starts ~8 min later, less time at extreme SoC.
        """
        if self.soc_target_reached(current_soc):
            return 0.0
        direction = self.kind.direction
        if direction.is_discharge:
            return seconds_for_range(self.target_soc, current_soc, direction.rate_zones)
        return seconds_for_range(current_soc, self.target_soc, direction.rate_zones)

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
# OneShotOperation — synthetic ad-hoc engagement (highest precedence)
# ─────────────────────────────────────────────────────────────────────────────


OneShotDisengageReason = Literal["target_reached", "expired", "cancelled"]


@dataclass(frozen=True)
class OneShotOperation:
    """Active ad-hoc battery operation overriding scheduled slots.

    Created when user presses "Execute" — lives in `BatterySchedule._oneshot`
    until target_reached/expired (auto-clear in compute_operation) or
    cancelled (user button). Precedence #0 — beats every scheduled slot.

    Uses absolute datetimes (not time-of-day) so it handles cross-midnight
    cleanly: user can set end_time=06:00 at 22:00 today, aggregate combines
    into tomorrow 06:00 when creating this VO.
    """

    direction: Direction
    target_soc: float
    end_at: datetime
    started_at: datetime
    # Always NORMAL — deliberate user action, voice escalation at arbitrary
    # hours is disruptive. Not configurable in UI. If EMERGENCY semantics
    # needed for evening peak, use scheduled slot DISCHARGE_EVENING (where
    # SlotProfile carries notification_level=EMERGENCY).
    notification_level: NotificationLevel = NotificationLevel.NORMAL

    def __post_init__(self) -> None:
        if not 0.0 <= self.target_soc <= 100.0:
            raise ValueError(f"target_soc {self.target_soc} outside [0, 100]")
        if self.end_at <= self.started_at:
            raise ValueError(
                f"end_at {self.end_at} must be after started_at {self.started_at}"
            )

    def is_expired(self, now: datetime) -> bool:
        return now >= self.end_at

    def target_reached(self, current_soc: float) -> bool:
        if self.direction.is_discharge:
            return current_soc <= self.target_soc
        return current_soc >= self.target_soc

    def to_battery_operation(self) -> BatteryOperation:
        """Build BatteryOperation (output) from this active one-shot.

        Symmetric with `BatteryScheduleEntry.to_battery_operation` — keeps
        the "how a source translates to BatteryOperation" logic with the
        source itself (Tell-Don't-Ask), not on BatteryOperation.
        """
        d = self.direction
        return BatteryOperation(
            ems_op=EmsOperation(
                ems_mode=d.ems_mode,
                power_limit_w=d.power_limit_w,
                source="schedule",
                reason=f"oneshot={d.name}",
            ),
            needs_charge_toggle=d.needs_charge_toggle,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction.name,
            "target_soc": self.target_soc,
            "end_at": self.end_at.isoformat(),
            "started_at": self.started_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OneShotOperation | None:
        try:
            direction = DISCHARGE if data["direction"] == "DISCHARGE" else CHARGE
            return cls(
                direction=direction,
                target_soc=float(data["target_soc"]),
                end_at=datetime.fromisoformat(data["end_at"]),
                started_at=datetime.fromisoformat(data["started_at"]),
            )
        except (KeyError, ValueError, TypeError):
            return None


@dataclass(frozen=True)
class OneShotParams:
    """User-editable defaults for one-shot operations (per direction).

    `end_time` is time-of-day; aggregate combines it with current date when
    starting a one-shot. If end_time <= now.time(), aggregate rolls to next
    day (e.g. discharge until 06:00 started at 22:00 ends tomorrow 06:00).
    """

    target_soc: float
    end_time: time

    def __post_init__(self) -> None:
        if not 0.0 <= self.target_soc <= 100.0:
            raise ValueError(f"target_soc {self.target_soc} outside [0, 100]")

    def with_target_soc(self, value: float) -> OneShotParams:
        return dataclasses.replace(self, target_soc=value)

    def with_end_time(self, value: time) -> OneShotParams:
        return dataclasses.replace(self, end_time=value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_soc": self.target_soc,
            "end_time": self.end_time.isoformat(timespec="minutes"),
        }

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], *, default: OneShotParams
    ) -> OneShotParams:
        try:
            return cls(
                target_soc=float(data.get("target_soc", default.target_soc)),
                end_time=time.fromisoformat(
                    data.get("end_time", default.end_time.isoformat(timespec="minutes"))
                ),
            )
        except (ValueError, TypeError):
            return default


# ─────────────────────────────────────────────────────────────────────────────
# Commands — mutating actions for slot entries (Command pattern)
# ─────────────────────────────────────────────────────────────────────────────


Scope = Literal["today", "tomorrow"]


class SlotCommand(Protocol):
    """A mutating action targeting a single slot entry in the aggregate.

    Aggregate calls `apply_to_entry(current)` to obtain the new Entry value;
    aggregate owns the read-modify-write lifecycle, Command owns the
    transformation. Adding a new editable field = new Command class (no
    changes to aggregate or service — Open/Closed).
    """

    scope: Scope
    kind: SlotKind

    def apply_to_entry(self, entry: BatteryScheduleEntry) -> BatteryScheduleEntry: ...


@dataclass(frozen=True)
class SetSlotEnabledCommand:
    scope: Scope
    kind: SlotKind
    value: bool

    def apply_to_entry(self, entry: BatteryScheduleEntry) -> BatteryScheduleEntry:
        return entry.with_enabled(self.value)


@dataclass(frozen=True)
class SetSlotStartCommand:
    scope: Scope
    kind: SlotKind
    value: time

    def apply_to_entry(self, entry: BatteryScheduleEntry) -> BatteryScheduleEntry:
        return entry.with_start(self.value)


@dataclass(frozen=True)
class SetSlotEndCommand:
    scope: Scope
    kind: SlotKind
    value: time

    def apply_to_entry(self, entry: BatteryScheduleEntry) -> BatteryScheduleEntry:
        return entry.with_end(self.value)


@dataclass(frozen=True)
class SetSlotTargetSocCommand:
    scope: Scope
    kind: SlotKind
    value: float

    def apply_to_entry(self, entry: BatteryScheduleEntry) -> BatteryScheduleEntry:
        return entry.with_target_soc(self.value)


@dataclass(frozen=True)
class SetSlotBehaviorCommand:
    scope: Scope
    kind: SlotKind
    value: SlotBehavior

    def apply_to_entry(self, entry: BatteryScheduleEntry) -> BatteryScheduleEntry:
        return entry.with_behavior(self.value)


# ─────────────────────────────────────────────────────────────────────────────
# One-shot Commands — operate on aggregate (lifecycle) or params (apply_to_params)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StartOneShotCommand:
    """Execute one-shot operation in given direction using stored params."""

    direction: Direction


@dataclass(frozen=True)
class CancelOneShotCommand:
    """Cancel active one-shot operation (no-op if none)."""


class OneShotParamsCommand(Protocol):
    """A mutating action targeting OneShotParams for one direction.

    Same pattern as `SlotCommand.apply_to_entry`: Command owns the
    transformation, aggregate owns the read-modify-write lifecycle and
    dict storage. New editable param field = new Command class (no
    changes to aggregate or service — Open/Closed).
    """

    direction: Direction

    def apply_to_params(self, params: OneShotParams) -> OneShotParams: ...


@dataclass(frozen=True)
class SetOneShotTargetSocCommand:
    direction: Direction
    value: float

    def apply_to_params(self, params: OneShotParams) -> OneShotParams:
        return params.with_target_soc(self.value)


@dataclass(frozen=True)
class SetOneShotEndTimeCommand:
    direction: Direction
    value: time

    def apply_to_params(self, params: OneShotParams) -> OneShotParams:
        return params.with_end_time(self.value)


# ─────────────────────────────────────────────────────────────────────────────
# BatterySchedule — aggregate root
# ─────────────────────────────────────────────────────────────────────────────


def _default_today() -> dict[SlotKind, BatteryScheduleEntry]:
    return {k: BatteryScheduleEntry.default_for(k) for k in SlotKind}


def _default_tomorrow() -> dict[SlotKind, BatteryScheduleEntry]:
    return {k: BatteryScheduleEntry.default_for(k) for k in SlotKind}


def _default_oneshot_params() -> dict[Direction, OneShotParams]:
    return {
        DISCHARGE: OneShotParams(target_soc=10.0, end_time=time(22, 0)),
        CHARGE: OneShotParams(target_soc=100.0, end_time=time(6, 0)),
    }


def _restore_oneshot_params(
    data: dict[str, Any],
) -> dict[Direction, OneShotParams]:
    """Restore one-shot params dict from persisted state with backward compat.

    Preferred format (current): nested under "oneshot_params" keyed by
    Direction.name. Falls back to legacy flat keys ("discharge_oneshot_params"
    / "charge_oneshot_params") from pre-dict-refactor deploys.
    """
    defaults = _default_oneshot_params()
    nested = data.get("oneshot_params")
    if nested:
        return {
            DISCHARGE: OneShotParams.from_dict(
                nested.get("DISCHARGE", {}), default=defaults[DISCHARGE]
            ),
            CHARGE: OneShotParams.from_dict(
                nested.get("CHARGE", {}), default=defaults[CHARGE]
            ),
        }
    # Legacy flat keys — restore from pre-refactor format if present.
    return {
        DISCHARGE: OneShotParams.from_dict(
            data.get("discharge_oneshot_params", {}), default=defaults[DISCHARGE]
        ),
        CHARGE: OneShotParams.from_dict(
            data.get("charge_oneshot_params", {}), default=defaults[CHARGE]
        ),
    }


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

    _today: dict[SlotKind, BatteryScheduleEntry] = field(default_factory=_default_today)
    _tomorrow: dict[SlotKind, BatteryScheduleEntry] = field(
        default_factory=_default_tomorrow
    )
    last_seen_date: date | None = None
    _currently_engaging: SlotKind | None = None
    _interventions_blocked_override: bool = False
    # When a slot or one-shot disengages, records the timestamp so consumers
    # can ask `is_active_this_hour(now)` — semantics matching legacy HA-side
    # template `binary_sensor.ems_other_automation_active_this_hour` that
    # GridExportManager uses to step aside in the clock hour following any
    # smart_rce intervention.
    _last_disengaged_at: datetime | None = None
    # One-shot operation state: when set, beats every scheduled slot. Auto-
    # clears in compute_operation on target_reached / expired, or via
    # cancel_oneshot() on user button.
    _oneshot: OneShotOperation | None = None
    _oneshot_params: dict[Direction, OneShotParams] = field(
        default_factory=_default_oneshot_params
    )

    @property
    def ems_interventions_blocked(self) -> bool:
        """True when smart_rce's internal interventions should step aside.

        Any of: user manually flipped the override, orchestrator engaged a
        scheduled slot, or a one-shot operation is active. All three make
        DodPolicy stay at DoD=90 and GridExportManager step aside.
        """
        return (
            self._interventions_blocked_override
            or self._currently_engaging is not None
            or self._oneshot is not None
        )

    @property
    def ems_interventions_blocked_override(self) -> bool:
        """User-controlled override flag (independent of slot engagement).

        The combined `ems_interventions_blocked` property is True when EITHER
        the user flipped this override OR a slot is currently engaging. This
        accessor exposes only the user-driven half.
        """
        return self._interventions_blocked_override

    @property
    def currently_engaging(self) -> SlotKind | None:
        """Slot currently being executed by the orchestrator (None when idle)."""
        return self._currently_engaging

    @property
    def oneshot(self) -> OneShotOperation | None:
        """Active one-shot operation, or None when idle."""
        return self._oneshot

    def oneshot_params(self, direction: Direction) -> OneShotParams:
        """User-editable one-shot defaults for the given direction."""
        return self._oneshot_params[direction]

    def current_operation(self) -> BatteryOperation:
        """Read-only snapshot of the BatteryOperation implied by current state.

        Precedence mirrors `compute_operation`: one-shot > scheduled engaging
        slot > idle. Pure read — no mutation. Use case: post-reload
        reconstruction of `BatteryScheduleService._last_op` (before first
        `compute_operation` tick has a chance to set it). compute_operation
        keeps its own inline branches because it also mutates aggregate state
        on engage/disengage transitions.
        """
        if self._oneshot is not None:
            return self._oneshot.to_battery_operation()
        if self._currently_engaging is not None:
            entry = self._today[self._currently_engaging]
            return entry.to_battery_operation()
        return BatteryOperation.idle()

    def set_ems_interventions_blocked_override(self, value: bool) -> bool:
        """Idempotent mutator for the user-controlled override flag — True if changed."""
        if self._interventions_blocked_override == value:
            return False
        self._interventions_blocked_override = value
        return True

    def is_active_this_hour(self, now: datetime) -> bool:
        """Return True if a slot/one-shot is engaging OR disengaged within current clock hour.

        Replaces HA-side `binary_sensor.ems_other_automation_active_this_hour`
        signal (Etap C). Used by `GridExportManager.update` to step aside in
        the post-intervention cleanup window (rest of the clock hour after a
        smart_rce slot disengaged — avoids racing the inverter back to
        intervention state immediately after we cleaned up).
        """
        if self._currently_engaging is not None or self._oneshot is not None:
            return True
        if self._last_disengaged_at is None:
            return False
        return self._last_disengaged_at.replace(
            minute=0, second=0, microsecond=0
        ) == now.replace(minute=0, second=0, microsecond=0)

    # ─── One-shot lifecycle ───

    def start_oneshot(
        self, direction: Direction, now: datetime
    ) -> list[BatteryScheduleEvent]:
        """Start one-shot using stored params for given direction. Emits OneShotStarted.

        No-op if a one-shot is already active (returns empty events list).
        Builds end_at by combining stored `end_time` (time-of-day) with
        `now.date()`. If end_time <= now.time(), rolls to next day — handles
        cross-midnight ("discharge until 06:00" started at 22:00 → tomorrow
        06:00).
        """
        if self._oneshot is not None:
            return []
        params = self._oneshot_params[direction]
        end_at = datetime.combine(now.date(), params.end_time, tzinfo=now.tzinfo)
        if end_at <= now:
            end_at = end_at + timedelta(days=1)
        op = OneShotOperation(
            direction=direction,
            target_soc=params.target_soc,
            end_at=end_at,
            started_at=now,
        )
        self._oneshot = op
        return [OneShotStarted(operation=op, at=now)]

    def cancel_oneshot(self, now: datetime) -> list[BatteryScheduleEvent]:
        """Cancel active one-shot. Emits OneShotEnded(reason='cancelled') if was active."""
        if self._oneshot is None:
            return []
        cancelled = self._oneshot
        self._oneshot = None
        self._last_disengaged_at = now
        return [OneShotEnded(operation=cancelled, reason="cancelled", at=now)]

    def apply_oneshot_command(self, cmd: OneShotParamsCommand) -> bool:
        """Update stored one-shot params via Command. True if changed.

        Aggregate owns the dict storage; Command owns the transformation
        (`apply_to_params`). New editable param field = new Command class
        with no change here (Open/Closed).
        """
        current = self._oneshot_params[cmd.direction]
        new = cmd.apply_to_params(current)
        if new == current:
            return False
        self._oneshot_params[cmd.direction] = new
        return True

    def today_entries(self) -> dict[SlotKind, BatteryScheduleEntry]:
        return dict(self._today)

    def tomorrow_entries(self) -> dict[SlotKind, BatteryScheduleEntry]:
        return dict(self._tomorrow)

    def today_entry_for(self, kind: SlotKind) -> BatteryScheduleEntry:
        return self._today[kind]

    def tomorrow_entry_for(self, kind: SlotKind) -> BatteryScheduleEntry:
        return self._tomorrow[kind]

    def apply_slot_command(self, cmd: SlotCommand) -> bool:
        """Apply a slot Command to the targeted entry. True if entry changed.

        Aggregate owns the read-modify-write lifecycle and dict storage;
        Command owns the transformation (`apply_to_entry`). New editable
        field = new Command class (no changes here). Caller is responsible
        for persisting via repo.save_if_changed().

        `BatteryScheduleEntry.__post_init__` validates invariants (target_soc
        range, start < end when enabled) and raises ValueError on bad input.
        """
        target = self._today if cmd.scope == "today" else self._tomorrow
        current = target[cmd.kind]
        new_entry = cmd.apply_to_entry(current)
        if new_entry == current:
            return False
        target[cmd.kind] = new_entry
        return True

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
        # TODO: temporarily disabled — today's slots persist across midnight so
        # user's customizations apply every day until explicit edit. Tomorrow
        # tab in UI is currently vestigial. Switch to Option B (tomorrow =
        # deepcopy(today) on roll) when we want tomorrow-overrides back.
        if self.last_seen_date is not None and self.last_seen_date != now.date():
            events.append(DayRolled(from_date=self.last_seen_date, to_date=now.date()))
            # self.roll_day()  # disabled — see TODO above
        self.last_seen_date = now.date()

        # 2. One-shot — precedence #0 (beats every scheduled slot).
        # Auto-clears on target_reached/expired; falls through to scheduled
        # logic after clearing so a scheduled slot can immediately take over
        # if it's in-window.
        if self._oneshot is not None:
            reason = self._oneshot_disengage_reason(now, current_soc)
            if reason is None:
                return self._oneshot.to_battery_operation(), events
            events.append(OneShotEnded(operation=self._oneshot, reason=reason, at=now))
            self._oneshot = None
            self._last_disengaged_at = now

        # 3. Already engaging? Stick until target_reached / window_ended / disabled.
        if self._currently_engaging is not None:
            entry = self.today_entry_for(self._currently_engaging)
            disengage_reason = _disengage_reason(entry, now, current_soc)
            if disengage_reason is None:
                # Stay engaged — sticky hysteresis trumps DELAYED_TO_END flicker.
                return entry.to_battery_operation(), events
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

        # 4. Find highest-precedence slot that should engage NOW.
        entry = self._find_engaging_entry(now, current_soc)
        if entry is not None:
            events.append(SlotEngaged(slot=entry.kind, soc=current_soc, at=now))
            self._currently_engaging = entry.kind
            return entry.to_battery_operation(), events

        return BatteryOperation.idle(), events

    def _find_engaging_entry(
        self, now: datetime, current_soc: float
    ) -> BatteryScheduleEntry | None:
        today = self.today_entries()
        engaging = [
            today[k] for k in _PRECEDENCE if today[k].should_apply_now(now, current_soc)
        ]
        return engaging[-1] if engaging else None

    def _oneshot_disengage_reason(
        self, now: datetime, current_soc: float
    ) -> OneShotDisengageReason | None:
        if self._oneshot is None:
            return None
        if self._oneshot.is_expired(now):
            return "expired"
        if self._oneshot.target_reached(current_soc):
            return "target_reached"
        return None

    def roll_day(self) -> None:
        """Shift tomorrow_* → today_*, reset tomorrow_* to disabled defaults.

        Idempotent — orchestrator should compare `last_seen_date` and call
        once per midnight crossing (Etap 2A).
        """
        self._today = self._tomorrow
        self._tomorrow = _default_tomorrow()

    def to_dict(self) -> dict[str, Any]:
        return {
            "today": {k.name: e.to_dict() for k, e in self._today.items()},
            "tomorrow": {k.name: e.to_dict() for k, e in self._tomorrow.items()},
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
            "oneshot": self._oneshot.to_dict() if self._oneshot is not None else None,
            "oneshot_params": {
                d.name: p.to_dict() for d, p in self._oneshot_params.items()
            },
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

        oneshot: OneShotOperation | None = None
        if raw_oneshot := data.get("oneshot"):
            oneshot = OneShotOperation.from_dict(raw_oneshot)

        oneshot_params = _restore_oneshot_params(data)

        return cls(
            _today={k: _entry("today", k) for k in SlotKind},
            _tomorrow={k: _entry("tomorrow", k) for k in SlotKind},
            last_seen_date=last_seen_date,
            _currently_engaging=currently_engaging,
            _interventions_blocked_override=bool(
                data.get("interventions_blocked_override", False)
            ),
            _last_disengaged_at=last_disengaged_at,
            _oneshot=oneshot,
            _oneshot_params=oneshot_params,
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
    """Schedule output: EmsOperation + local battery management metadata.

    HAS-A `EmsOperation` (Goodwe inverter target — consumed by
    GoodweEmsActuator via `.ems_op`) plus `needs_charge_toggle` (local
    `switch.battery_charge_max_current_toggle` BMS guard — consumed by
    BatteryChargePolicy, separate concern from Goodwe writes).

    Composition over inheritance — schedule **produces** an EmsOperation
    + extra metadata; it is not itself an inverter target. Caller
    (`Ems._resolve_ems_operation`) extracts `.ems_op` when it needs the
    pure inverter target.

    `ems_op.source="schedule"` for both slot-driven and one-shot ops;
    `ems_op.reason` carries identity: `"slot=DISCHARGE_EVENING"` /
    `"oneshot=DISCHARGE"` / None when idle. Diagnostic-only — no
    programmatic parsing required.

    NO `ems_override_active` field — `schedule.ems_interventions_blocked`
    is the canonical source of truth (read by Ems body and passed
    explicitly to DodPolicy/GridExportManager). NO `dod_force` — DodPolicy
    reacts via `INTERVENTIONS_BLOCKED` phase.
    """

    ems_op: EmsOperation
    needs_charge_toggle: bool = False

    @property
    def is_idle(self) -> bool:
        """Forward to ems_op — engagement is driven by inverter target state."""
        return self.ems_op.is_idle

    @classmethod
    def idle(cls) -> BatteryOperation:
        return cls(ems_op=EmsOperation.neutral(), needs_charge_toggle=False)

    # Sources construct BatteryOperation themselves via
    # `BatteryScheduleEntry.to_battery_operation()` and
    # `OneShotOperation.to_battery_operation()` — keeps the translation
    # knowledge with the source class (Tell-Don't-Ask), so BatteryOperation
    # doesn't have to know its possible sources.


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


@dataclass(frozen=True)
class OneShotStarted(BatteryScheduleEvent):
    """One-shot operation just started — user pressed Execute."""

    operation: OneShotOperation
    at: datetime


@dataclass(frozen=True)
class OneShotEnded(BatteryScheduleEvent):
    """One-shot operation ended.

    `reason` distinguishes:
    - "target_reached" — SoC reached target (normal completion)
    - "expired" — `now >= end_at` reached
    - "cancelled" — user pressed Cancel button
    """

    operation: OneShotOperation
    reason: OneShotDisengageReason
    at: datetime
