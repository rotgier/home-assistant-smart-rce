"""BatterySchedule aggregate root.

Drives `ems_interventions_blocked` and produces `BatteryOperation` per tick.

Eight named slots — four per day (today / tomorrow):

- CHARGE_MORNING      (cheap night/early-morning RCE hours, ~02-06)
- DISCHARGE_MORNING   (secondary morning peak; PV+battery hybrid via DISCHARGE_PV)
- CHARGE_AFTERNOON    (cheap afternoon RCE — pre-fill battery before evening
                       peak if PV is insufficient. April-September: 13-19;
                       other months: 13-16. User sets window manually per season.)
- DISCHARGE_EVENING   (primary RCE peak; voice-call notification OK)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .commands import OneShotParamsCommand, SlotCommand
from .direction import Direction
from .entry import BatteryScheduleEntry, SlotKind
from .events import (
    BatteryScheduleEvent,
    DayRolled,
    OneShotEnded,
    OneShotStarted,
    SlotDisengaged,
    SlotEngaged,
)
from .oneshot import OneShotDisengageReason, OneShotOperation, OneShotParams
from .operation import BatteryOperation


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
