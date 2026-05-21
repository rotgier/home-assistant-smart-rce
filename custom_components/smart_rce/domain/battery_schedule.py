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
from datetime import date, time
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
        )
