"""Battery schedule — user/proposer intent for daily charge/discharge windows.

`BatterySchedule` (top of file) is the aggregate root — 4 named slots × 2 days
+ optional one-shot operation. It drives `ems_interventions_blocked` (consumed
by DodPolicy + GridExportManager) and produces `BatteryOperation` per tick.

Eight named slots — four per day (today / tomorrow):

- CHARGE_MORNING      (cheap night/early-morning RCE hours, ~02-06)
- DISCHARGE_MORNING   (secondary morning peak; PV+battery hybrid via DISCHARGE_PV)
- CHARGE_AFTERNOON    (cheap afternoon RCE — pre-fill battery before evening
                       peak if PV is insufficient. April-September: 13-19;
                       other months: 13-16. User sets window manually per season.)
- DISCHARGE_EVENING   (primary RCE peak; voice-call notification OK)

Behavior model:
- `SlotBehavior.IMMEDIATE`      — engage at `start`, stop at target_soc/end
- `SlotBehavior.DELAYED_TO_END` — delay start so target_soc is reached just
                                  before `end`. Default.

File structure note: `BatterySchedule` is at the top (main aggregate). Direction
+ RateZone come next because `SlotKind` enum values eagerly bind
`SlotProfile(direction=Direction.X)`, and SlotProfile must precede SlotKind for
the same reason. Within "Slot domain", `BatteryScheduleEntry` is the main concept
but `SlotBehavior` precedes it (eager default for `behavior` field). This is the
documented eager-eval compromise from `docs/code-style.md`.

Reload safety: enum members are not stable across `live_reload()` (re-imported
enum class gives new member identity). For comparisons that must survive reload,
use `direction.is_discharge` / `direction.is_charge` (string-name compare) or
`direction.name == "DISCHARGE"`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum, StrEnum
from typing import Any, Literal, Protocol

from .ems_operation import EmsMode, EmsOperation

# ═════════════════════════════════════════════════════════════════════════════
#  BatterySchedule — aggregate root
# ═════════════════════════════════════════════════════════════════════════════


@dataclass
class BatterySchedule:
    """Aggregate root — 4 named slots × 2 days = 8 entries + optional one-shot.

    Plus two state fields that drive `ems_interventions_blocked`:
    - `_currently_engaging` (persisted): which slot is currently being executed
      by the orchestrator (set by `compute_operation`).
    - `_interventions_blocked_override` (persisted): user-driven manual override
      from `switch.ems_interventions_blocked`.

    Combined via OR with `_oneshot is not None`: any reason → interventions
    blocked → DodPolicy stays at DoD=90 and GridExportManager steps aside.

    Midnight roll: tomorrow_* → today_*, tomorrow_* reset to disabled defaults.
    Implemented in `roll_day()`; called by `compute_operation` on date change
    (currently disabled — today's slots persist across midnight).
    """

    _today: dict[SlotKind, BatteryScheduleEntry] = field(
        default_factory=lambda: BatteryScheduleEntry.defaults_for_all_kinds()
    )
    _tomorrow: dict[SlotKind, BatteryScheduleEntry] = field(
        default_factory=lambda: BatteryScheduleEntry.defaults_for_all_kinds()
    )
    last_seen_date: date | None = None
    _currently_engaging: SlotKind | None = None
    _interventions_blocked_override: bool = False
    # When a slot or one-shot disengages, records the timestamp so consumers
    # can ask `is_active_this_hour(now)` — GridExportManager uses this to
    # step aside in the clock hour following any smart_rce intervention.
    _last_disengaged_at: datetime | None = None
    # One-shot operation state: when set, beats every scheduled slot. Auto-
    # clears in compute_operation on target_reached / expired, or via
    # cancel_oneshot() on user button.
    _oneshot: OneShotOperation | None = None
    _oneshot_params: dict[Direction, OneShotParams] = field(
        default_factory=lambda: OneShotParams.defaults_by_direction()
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

        Used by `GridExportManager.update` to step aside in the post-intervention
        cleanup window (rest of the clock hour after a smart_rce slot disengaged
        — avoids racing the inverter back to intervention state immediately
        after we cleaned up).
        """
        if self._currently_engaging is not None or self._oneshot is not None:
            return True
        if self._last_disengaged_at is None:
            return False
        return self._last_disengaged_at.replace(
            minute=0, second=0, microsecond=0
        ) == now.replace(minute=0, second=0, microsecond=0)

    # ─── Slot accessors ───

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
        return [
            OneShotEnded(
                operation=cancelled,
                reason=OneShotDisengageReason.CANCELLED,
                at=now,
            )
        ]

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
        (see `SlotKind.by_precedence()` — last wins as strongest).
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
            disengage_reason = entry.disengage_reason(now, current_soc)
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
            today[k]
            for k in SlotKind.by_precedence()
            if today[k].should_apply_now(now, current_soc)
        ]
        return engaging[-1] if engaging else None

    def _oneshot_disengage_reason(
        self, now: datetime, current_soc: float
    ) -> OneShotDisengageReason | None:
        if self._oneshot is None:
            return None
        if self._oneshot.is_expired(now):
            return OneShotDisengageReason.EXPIRED
        if self._oneshot.target_reached(current_soc):
            return OneShotDisengageReason.TARGET_REACHED
        return None

    def roll_day(self) -> None:
        """Shift tomorrow_* → today_*, reset tomorrow_* to disabled defaults.

        Idempotent — orchestrator should compare `last_seen_date` and call
        once per midnight crossing.
        """
        self._today = self._tomorrow
        self._tomorrow = BatteryScheduleEntry.defaults_for_all_kinds()

    # ─── Persistence ───

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
            _oneshot_params=OneShotParams.restore_by_direction(data),
        )


# ═════════════════════════════════════════════════════════════════════════════
#  Direction + rate model (eager deps of SlotKind below)
# ═════════════════════════════════════════════════════════════════════════════


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

    def overlap_seconds(self, low: float, high: float) -> float:
        """Seconds contributed by this zone for SoC traversal `[low, high)`.

        Returns 0 when the zone doesn't overlap the requested range.
        """
        ol = max(low, self.soc_from)
        oh = min(high, self.soc_to)
        return (oh - ol) * self.sec_per_pp if oh > ol else 0.0


class Direction(Enum):
    """Battery flow direction (DISCHARGE / CHARGE) + per-direction settings.

    Enum with bound metadata: EMS mode, power limit, charge-toggle requirement,
    SoC-zone rate model. Every `SlotKind` references one via `SlotProfile`.

    DISCHARGE rate zones — empirical from 2026-05-04 morning discharge session
    (DISCHARGE_BATTERY @ 6kW, BMS ~5kW effective). FAST ZONE 25-100 covers
    normal evening discharge 100→30; below 25 we hit BMS quirks (compressed
    mid-range, calibration pause 14-16, fast end below 14).

    CHARGE rate zones — no empirical data yet, uniform 75 sec/pp stub.
    TODO: collect empirical data + replace with zones analogous to discharge.

    Comparison: NEVER use `is` across `live_reload()` boundary (re-imported
    enum class gives new member identity). Use `direction.is_discharge` /
    `direction.is_charge` (name-based) for live_reload safety. Within a
    single import lifetime, `direction is Direction.DISCHARGE` is fine.
    """

    DISCHARGE = (
        # PV+battery hybrid. Morning: PV covers load, battery supplies overflow.
        # Evening: PV is zero, mode degrades to battery-only — same effect as
        # DISCHARGE_BATTERY without needing a second EMS mode in the matrix.
        EmsMode.DISCHARGE_PV,
        6000,
        False,
        (
            RateZone(soc_from=25.0, soc_to=100.01, sec_per_pp=75.0),
            RateZone(soc_from=16.0, soc_to=25.0, sec_per_pp=36.0),
            RateZone(soc_from=14.0, soc_to=16.0, sec_per_pp=97.0),
            RateZone(soc_from=0.0, soc_to=14.0, sec_per_pp=34.0),
        ),
    )
    CHARGE = (
        EmsMode.CHARGE_BATTERY,
        6000,
        True,
        (RateZone(soc_from=0.0, soc_to=100.01, sec_per_pp=75.0),),
    )

    def __init__(
        self,
        ems_mode: EmsMode,
        power_limit_w: int,
        needs_charge_toggle: bool,
        rate_zones: tuple[RateZone, ...],
    ) -> None:
        self.ems_mode = ems_mode
        self.power_limit_w = power_limit_w
        self.needs_charge_toggle = needs_charge_toggle
        self.rate_zones = rate_zones

    @property
    def is_discharge(self) -> bool:
        return self.name == "DISCHARGE"

    @property
    def is_charge(self) -> bool:
        return self.name == "CHARGE"

    def seconds_for_soc_traversal(self, start_soc: float, end_soc: float) -> float:
        """Sum sec_per_pp across rate zones covering the SoC traversal start→end.

        Direction-agnostic — internally normalizes to [low, high] so callers
        can pass `(current_soc, target_soc)` regardless of charge/discharge.
        Returns 0 when start == end. SoC outside zone coverage contributes 0.
        """
        low, high = min(start_soc, end_soc), max(start_soc, end_soc)
        if low >= high:
            return 0.0
        return sum(z.overlap_seconds(low, high) for z in self.rate_zones)


# ═════════════════════════════════════════════════════════════════════════════
#  Slot domain — entry (main) + supporting kind/profile/behavior/notification
# ═════════════════════════════════════════════════════════════════════════════


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

    @classmethod
    def defaults_for_all_kinds(cls) -> dict[SlotKind, BatteryScheduleEntry]:
        """Disabled-default entry for every `SlotKind` — used by aggregate factory."""
        return {k: cls.default_for(k) for k in SlotKind}

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

    def sec_until_end(self, now: datetime) -> float:
        """Seconds from `now` until today's `end` time. Negative if already past."""
        end_dt = datetime.combine(now.date(), self.end, tzinfo=now.tzinfo)
        return (end_dt - now).total_seconds()

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
        return self.sec_until_end(now) <= self.time_to_complete_at(current_soc)

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


# ═════════════════════════════════════════════════════════════════════════════
#  One-shot — synthetic ad-hoc engagement (highest precedence)
# ═════════════════════════════════════════════════════════════════════════════


class OneShotDisengageReason(StrEnum):
    """Why a one-shot operation stopped — used by OneShotEnded event."""

    TARGET_REACHED = "target_reached"
    """SoC reached target (normal completion)."""

    EXPIRED = "expired"
    """`now >= end_at` reached — window timeout."""

    CANCELLED = "cancelled"
    """User pressed Cancel button."""


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
            return cls(
                direction=Direction[data["direction"]],
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

    @classmethod
    def defaults_by_direction(cls) -> dict[Direction, OneShotParams]:
        """Build default params per direction — used by aggregate's field factory.

        DISCHARGE: end at 22:00 with target SoC 10% (evening peak default).
        CHARGE: end at 06:00 with target SoC 100% (overnight cheap-rate fill).
        """
        return {
            Direction.DISCHARGE: cls(target_soc=10.0, end_time=time(22, 0)),
            Direction.CHARGE: cls(target_soc=100.0, end_time=time(6, 0)),
        }

    @classmethod
    def restore_by_direction(
        cls, data: dict[str, Any]
    ) -> dict[Direction, OneShotParams]:
        """Restore params dict from persisted state with backward compat.

        Preferred format (current): nested under "oneshot_params" keyed by
        `Direction.name`. Falls back to legacy flat keys
        ("discharge_oneshot_params" / "charge_oneshot_params") from
        pre-dict-refactor deploys. Used by `BatterySchedule.from_dict`.
        """
        defaults = cls.defaults_by_direction()
        nested = data.get("oneshot_params")
        if nested:
            return {
                Direction.DISCHARGE: cls.from_dict(
                    nested.get("DISCHARGE", {}),
                    default=defaults[Direction.DISCHARGE],
                ),
                Direction.CHARGE: cls.from_dict(
                    nested.get("CHARGE", {}),
                    default=defaults[Direction.CHARGE],
                ),
            }
        # Legacy flat keys — restore from pre-refactor format if present.
        return {
            Direction.DISCHARGE: cls.from_dict(
                data.get("discharge_oneshot_params", {}),
                default=defaults[Direction.DISCHARGE],
            ),
            Direction.CHARGE: cls.from_dict(
                data.get("charge_oneshot_params", {}),
                default=defaults[Direction.CHARGE],
            ),
        }

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


# ═════════════════════════════════════════════════════════════════════════════
#  Commands — mutating actions (Command pattern, Open/Closed)
# ═════════════════════════════════════════════════════════════════════════════


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


# ═════════════════════════════════════════════════════════════════════════════
#  Inputs / Outputs
# ═════════════════════════════════════════════════════════════════════════════


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


# ═════════════════════════════════════════════════════════════════════════════
#  Events — domain happenings emitted by compute_operation
# ═════════════════════════════════════════════════════════════════════════════


class DisengageReason(StrEnum):
    """Why a scheduled slot stopped engaging — used by SlotDisengaged event.

    None (from `Entry.disengage_reason`) = keep engaging.
    """

    TARGET_REACHED = "target_reached"
    """SoC reached target (normal completion)."""

    WINDOW_ENDED = "window_ended"
    """`now` moved past `end` (window timeout)."""

    DISABLED = "disabled"
    """`slot.enabled` flipped to False mid-engagement."""


@dataclass(frozen=True)
class BatteryScheduleEvent:
    """Base marker class for domain events emitted by compute_operation.

    Notifier dispatches by isinstance — separate handler per type.
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
