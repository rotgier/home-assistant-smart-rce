"""BatterySchedule aggregate root.

Drives `ems_interventions_blocked` and produces `BatteryOperation` per tick.

Eight named slots — four per day (today / tomorrow):

- CHARGE_MORNING      (cheap night/early-morning RCE hours, ~02-06)
- DISCHARGE_MORNING   (secondary morning peak; PV+battery hybrid via DISCHARGE_PV)
- CHARGE_AFTERNOON    (cheap afternoon RCE — pre-fill battery before evening
                       peak if PV is insufficient. April-September: 13-19;
                       other months: 13-16. User sets window manually per season.)
- DISCHARGE_EVENING_EARLY  (primary RCE peak; voice-call notification OK)
- DISCHARGE_EVENING_LATE   (second evening window — a later expensive hour
                            after a hold, e.g. peak at 19 then 21-22)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Final

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
from .t1_reserve import T1SocReserve

# Store key of the single evening slot that predates the EARLY/LATE split.
_LEGACY_EVENING_KEY = "DISCHARGE_EVENING"

# Slots the evening proposer owns — edits to these are what "hand-set" means.
_EVENING_KINDS = frozenset(
    {SlotKind.DISCHARGE_EVENING_EARLY, SlotKind.DISCHARGE_EVENING_LATE}
)


class BatterySchedule:
    """Aggregate root — one entry per `SlotKind` × 2 days + optional one-shot.

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

    A plain class rather than a dataclass: an aggregate is compared by identity,
    and the constructor takes exactly what `from_dict` restores.
    """

    CHECKPOINT_EVERY: Final[timedelta] = timedelta(minutes=15)

    def __init__(
        self,
        *,
        today: dict[SlotKind, BatteryScheduleEntry] | None = None,
        tomorrow: dict[SlotKind, BatteryScheduleEntry] | None = None,
        last_seen_date: date | None = None,
        currently_engaging: SlotKind | None = None,
        interventions_blocked_override: bool = False,
        last_disengaged_at: datetime | None = None,
        oneshot: OneShotOperation | None = None,
        oneshot_params: dict[Direction, OneShotParams] | None = None,
        evening_hand_set_day: date | None = None,
        t1_reserve: T1SocReserve | None = None,
    ) -> None:
        self._today: dict[SlotKind, BatteryScheduleEntry] = (
            dict(today)
            if today is not None
            else BatteryScheduleEntry.defaults_for_all_kinds()
        )
        self._tomorrow: dict[SlotKind, BatteryScheduleEntry] = (
            dict(tomorrow)
            if tomorrow is not None
            else BatteryScheduleEntry.defaults_for_all_kinds()
        )
        self.last_seen_date = last_seen_date
        self._currently_engaging = currently_engaging
        self._interventions_blocked_override = interventions_blocked_override
        # When a slot or one-shot disengages, records the timestamp so consumers
        # can ask `is_active_this_hour(now)` — GridExportManager uses this to
        # step aside in the clock hour following any smart_rce intervention.
        self._last_disengaged_at = last_disengaged_at
        # One-shot operation state: when set, beats every scheduled slot. Auto-
        # clears in compute_operation on target_reached / expired, or via
        # cancel_oneshot() on user button.
        self._oneshot = oneshot
        self._oneshot_params: dict[Direction, OneShotParams] = (
            oneshot_params
            if oneshot_params is not None
            else OneShotParams.defaults_by_direction()
        )
        # Day on which the user last edited an evening slot by hand. The proposer
        # stands down while this equals today, so an automatic run never quietly
        # undoes a deliberate change. A date rather than a flag: it expires on its
        # own at midnight, with no clearing step that a restart could miss.
        self._evening_hand_set_day = evening_hand_set_day
        # Latest forecast-derived T1 reserve, kept with the day it is for. The
        # auto morning charge uses it as its target. Persisted so a restart in
        # the night — when the forecast has not been rebuilt yet — still charges
        # to the evening's figure rather than to the slot's fallback.
        self._t1_reserve = t1_reserve
        # What `to_dict` writes. Between 07:00 and 13:00 the reserve moves
        # nearly every minute; the night only needs the evening figure, so the
        # stored copy follows at most every CHECKPOINT_EVERY (immediately when
        # the day, variant or window changes).
        self._t1_reserve_checkpoint = t1_reserve
        self._t1_reserve_checkpoint_at: datetime | None = None

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

    def apply_manual_slot_command(self, cmd: SlotCommand, *, today: date) -> bool:
        """Apply a command the USER issued, marking evening slots as hand-set.

        Separate entry point from `apply_slot_command` so provenance follows
        from which method was called, rather than from a flag that a caller
        could forget to pass.
        """
        changed = self.apply_slot_command(cmd)
        if changed and cmd.scope == "today" and cmd.kind in _EVENING_KINDS:
            self._evening_hand_set_day = today
        return changed

    def evening_is_hand_set_on(self, day: date) -> bool:
        """Tell whether the user edited an evening slot by hand on `day`."""
        return self._evening_hand_set_day == day

    def release_evening_to_proposer(self) -> bool:
        """Drop the hand-set mark so automatic runs own the evening again."""
        if self._evening_hand_set_day is None:
            return False
        self._evening_hand_set_day = None
        return True

    def set_t1_reserve(self, reserve: T1SocReserve, now: datetime) -> bool:
        """Remember the latest T1 reserve. True if it changed.

        The stored checkpoint moves along only when the reserve switches day,
        variant or window, or once CHECKPOINT_EVERY has passed since the last.
        """
        if self._t1_reserve == reserve:
            return False
        self._t1_reserve = reserve
        if (
            not reserve.same_slot_as(self._t1_reserve_checkpoint)
            or self._t1_reserve_checkpoint_at is None
            or now - self._t1_reserve_checkpoint_at >= self.CHECKPOINT_EVERY
        ):
            self._t1_reserve_checkpoint = reserve
            self._t1_reserve_checkpoint_at = now
        return True

    def t1_reserve_for(self, day: date) -> T1SocReserve | None:
        """Return the remembered reserve when it is for `day`, else None."""
        reserve = self._t1_reserve
        return reserve if reserve is not None and reserve.day == day else None

    @property
    def t1_reserve(self) -> T1SocReserve | None:
        """Latest remembered reserve, whatever day it is for."""
        return self._t1_reserve

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
        self, now: datetime, current_soc: float, *, is_workday: bool | None = None
    ) -> tuple[BatteryOperation, list[BatteryScheduleEvent]]:
        """Decide BatteryOperation + emit domain events for what changed.

        Mutates aggregate state:
        - `last_seen_date` set to `now.date()` (rolls tomorrow→today on date change)
        - `_currently_engaging` flipped on engage/disengage

        Hysteresis: once `_currently_engaging` is set, keep engaging that slot
        until target_reached OR out-of-window OR slot disabled. Prevents
        flicker when SoC change rate diverges from the rate estimate.

        Precedence (when no current engagement, multiple slots in window):
        DISCHARGE_EVENING_EARLY > DISCHARGE_MORNING > CHARGE_AFTERNOON > CHARGE_MORNING
        > CHARGE_MORNING_AUTO (see `SlotKind.by_precedence()` — last wins).

        Auto-target slots run on an effective entry whose target is the
        remembered morning target (see `_effective_entry`), and stay out on a
        day `is_workday` reports as off. Unknown (None) counts as a workday:
        a needless charge costs only round-trip losses, a missed one a T1 hour.
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
            entry = self._effective_entry(self._currently_engaging, now.date())
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
                    target_soc=entry.target_soc,
                    target_source=self._target_source(entry.kind, now.date()),
                )
            )
            self._currently_engaging = None
            self._last_disengaged_at = now
            # Fall through — another slot might be ready to engage immediately.

        # 4. Find highest-precedence slot that should engage NOW.
        engaging_entry = self._find_engaging_entry(now, current_soc, is_workday)
        if engaging_entry is not None:
            events.append(
                SlotEngaged(
                    slot=engaging_entry.kind,
                    soc=current_soc,
                    at=now,
                    target_soc=engaging_entry.target_soc,
                    target_source=self._target_source(engaging_entry.kind, now.date()),
                )
            )
            self._currently_engaging = engaging_entry.kind
            return engaging_entry.to_battery_operation(), events

        return BatteryOperation.idle(), events

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

    def _find_engaging_entry(
        self, now: datetime, current_soc: float, is_workday: bool | None
    ) -> BatteryScheduleEntry | None:
        candidates = [
            self._effective_entry(k, now.date())
            for k in SlotKind.by_precedence()
            if not (k.profile.auto_target and is_workday is False)
        ]
        engaging = [e for e in candidates if e.should_apply_now(now, current_soc)]
        return engaging[-1] if engaging else None

    def _effective_entry(self, kind: SlotKind, day: date) -> BatteryScheduleEntry:
        """Today's entry as the orchestrator runs it.

        Auto-target kinds swap in the remembered T1 reserve for `day`;
        without one their own `target_soc` (the fallback) stands. Every other
        predicate — window, DELAYED_TO_END, target reached — is the entry's own.
        """
        entry = self._today[kind]
        if not kind.profile.auto_target:
            return entry
        reserve = self.t1_reserve_for(day)
        if reserve is None:
            return entry
        return entry.with_target_soc(float(reserve.value))

    def _target_source(self, kind: SlotKind, day: date) -> str | None:
        """Where an auto-target slot's target came from; None for fixed slots."""
        if not kind.profile.auto_target:
            return None
        reserve = self.t1_reserve_for(day)
        return reserve.source if reserve is not None else "fallback"

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
            "evening_hand_set_day": (
                self._evening_hand_set_day.isoformat()
                if self._evening_hand_set_day is not None
                else None
            ),
            "oneshot": self._oneshot.to_dict() if self._oneshot is not None else None,
            "t1_reserve": (
                self._t1_reserve_checkpoint.to_dict()
                if self._t1_reserve_checkpoint is not None
                else None
            ),
            "oneshot_params": {
                d.name: p.to_dict() for d, p in self._oneshot_params.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatterySchedule:
        def _entry(scope: str, kind: SlotKind) -> BatteryScheduleEntry:
            stored = data.get(scope) or {}
            payload = stored.get(kind.name)
            if payload is None and kind is SlotKind.DISCHARGE_EVENING_EARLY:
                # Migration: the single evening slot was split into EARLY +
                # LATE. Its saved window is the one the user actually tuned,
                # so it becomes EARLY; LATE starts from defaults.
                payload = stored.get(_LEGACY_EVENING_KEY)
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
            if engaging_name == _LEGACY_EVENING_KEY:
                engaging_name = SlotKind.DISCHARGE_EVENING_EARLY.name
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

        evening_hand_set_day: date | None = None
        if raw := data.get("evening_hand_set_day"):
            try:
                evening_hand_set_day = date.fromisoformat(raw)
            except (TypeError, ValueError):
                evening_hand_set_day = None

        t1_reserve: T1SocReserve | None = None
        # `morning_target` is the key the first release (2026-10-11) wrote.
        if raw_reserve := data.get("t1_reserve") or data.get("morning_target"):
            t1_reserve = T1SocReserve.from_dict(raw_reserve)

        oneshot: OneShotOperation | None = None
        if raw_oneshot := data.get("oneshot"):
            oneshot = OneShotOperation.from_dict(raw_oneshot)

        return cls(
            today={k: _entry("today", k) for k in SlotKind},
            tomorrow={k: _entry("tomorrow", k) for k in SlotKind},
            last_seen_date=last_seen_date,
            currently_engaging=currently_engaging,
            interventions_blocked_override=bool(
                data.get("interventions_blocked_override", False)
            ),
            last_disengaged_at=last_disengaged_at,
            oneshot=oneshot,
            oneshot_params=OneShotParams.restore_by_direction(data),
            evening_hand_set_day=evening_hand_set_day,
            t1_reserve=t1_reserve,
        )
