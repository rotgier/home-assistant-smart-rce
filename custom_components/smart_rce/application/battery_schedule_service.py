"""BatteryScheduleService — application orchestrator + use case facade.

Public API consumed by HA entities (switch, dashboard cards) and Ems:
- `update(input)` — per-tick orchestration: calls aggregate compute_operation,
  persists changed aggregate state, notifies listeners when domain events
  fired (preemptive — gotowe na Etap 2B observability sensors).
- `ems_interventions_blocked_override` — current user override state (property)
- `set_ems_interventions_blocked_override(value)` — UI-driven async toggle
- `add_listener(cb)` — single-registry refresh hook (inherited from `Service`)

Repo is an internal collaborator (persistence); not exposed externally.
Switch and other consumers reach BatterySchedule state through this service.

DDD application layer:
- HASS-unaware (no `hass`, no HA service calls)
- Dependencies injected via constructor (repo + clock + tasks)
- Use case methods compose domain mutations + repo persistence + listener
  notifications explicitly — no listener indirection on repo

Etap 2A scope (now): wires compute_operation through update, persists
aggregate, tracks last_op for diff detection. Adapter side effects deferred
to Etap 2D — events are computed but NOT dispatched yet (logged debug only).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Final

from homeassistant.core import callback

from ..domain.battery_schedule import (
    BatteryOperation,
    BatteryScheduleEntry,
    BatteryScheduleInput,
    Direction,
    MorningDischargePlan,
    OneShotOperation,
    OneShotParams,
    OneShotParamsCommand,
    ReserveWindow,
    Scope,
    SlotCommand,
    SlotKind,
    T1SocReserve,
)
from ..domain.evening_plan import EveningPlan
from ..domain.pv_forecast import PvForecast
from ..infrastructure.battery_schedule_repository import BatteryScheduleRepository
from .plan_application import PlanApplication, PlanOutcome, SlotChange
from .service import Service


@dataclass(frozen=True)
class BatteryScheduleUpdateResult:
    """Return value of `BatteryScheduleService.update`.

    Atomic capture of all values Ems needs to pass to downstream managers
    (DodPolicy, GridExport) this tick — taken AFTER compute_operation
    mutated the aggregate. Lazy property reads on the service still work
    for external consumers (sensors) — this is the orchestration-side
    return contract.
    """

    operation: BatteryOperation
    ems_interventions_blocked: bool
    ems_interventions_blocked_override: bool
    schedule_active_this_hour: bool


if TYPE_CHECKING:
    from ..domain.target_soc_catalog import TargetSocCatalog
    from ..infrastructure.async_task_runner import AsyncTaskRunner
    from ..infrastructure.battery_schedule_notifier import BatteryScheduleNotifier

_LOGGER = logging.getLogger(__name__)

# Slots the evening planner owns — the ones worth diffing and reporting.
_EVENING = (SlotKind.DISCHARGE_EVENING_EARLY, SlotKind.DISCHARGE_EVENING_LATE)


class BatteryScheduleService(Service[BatteryScheduleRepository]):
    """Application service. HASS-unaware — dependencies injected at construction.

    Use case methods (set_ems_interventions_blocked_override, add_listener)
    are the external API for HA entities. Repository stays internal.
    """

    # T1 (expensive zone, workdays) runs 07:00-13:00.
    _T1_START_HOUR: Final[int] = 7
    _T1_END_HOUR: Final[int] = 13
    # From this hour the reserve moves on to tomorrow's window.
    _TOMORROW_FROM_HOUR: Final[int] = 16

    def __init__(
        self,
        repo: BatteryScheduleRepository,
        clock: Callable[[], datetime],
        tasks: AsyncTaskRunner,  # noqa: ARG002 — kept for future applier wiring
        notifier: BatteryScheduleNotifier | None = None,
    ) -> None:
        super().__init__(repo)
        self._clock = clock
        self._notifier = notifier
        # Track last BatteryOperation to skip no-op applies (Etap 2D applier
        # uses this to avoid spurious Goodwe writes when the op didn't change).
        #
        # Reconstruct from persisted schedule state so the first post-reload
        # tick (which may have battery_soc=None during HA state cache warm-up)
        # returns the engagement that was active pre-reload rather than `idle()`.
        # Without this, a long-running engagement would briefly report `idle`
        # to downstream consumers (e.g. BatteryChargeService would flip
        # `needs_charge_toggle` False → write Modbus → flicker).
        self._last_op: BatteryOperation = self._repo.schedule.current_operation()
        # Today's calendar, refreshed each tick — decides where the evening
        # ends, and so which evening a hand edit is aimed at.
        self._last_is_workday: bool | None = None
        # LIVE+EXTRAP reserve — observation only, nothing acts on it yet.
        self._t1_reserve_extrap: T1SocReserve | None = None

    @callback
    def update(self, input: BatteryScheduleInput) -> BatteryScheduleUpdateResult:
        """Per-tick orchestration — called from Ems.update_state.

        Returns a snapshot of all values Ems needs to drive downstream managers
        (operation, ems_interventions_blocked, schedule_active_this_hour) —
        captured atomically AFTER compute_operation mutated the aggregate.

        Fires `_notify_all()` when compute_operation emitted events (slot
        engage/disengage/day_rolled) — preemptive for Etap 2B observability
        sensors that will observe `_currently_engaging` state directly.
        """
        self._last_is_workday = input.is_workday
        if input.battery_soc is None:
            return self._make_result(self._last_op)

        # ─── Domain logic — aggregate decides operation + emits events ───
        op, events = self._repo.schedule.compute_operation(
            self._clock(), input.battery_soc, is_workday=input.is_workday
        )

        # ─── Persistence — repo save_if_changed checks dict equality internally ───
        self._repo.save_if_changed()

        # ─── Events — log + telegram (notifier) + listener fan-out ───
        # Notifier dispatch is fire-and-forget (background task) — slot
        # engage/disengage telegrams must not block per-tick update flow.
        # _notify_all() refreshes UI listeners (sensors observing
        # `_currently_engaging` state directly).
        for event in events:
            _LOGGER.info("BatteryScheduleEvent: %s", event)
            if self._notifier is not None:
                self._notifier.notify(event)
        if events:
            self._notify_all()

        # ─── Apply — only when op changed; applier wired in Etap 2D ───
        if op != self._last_op:
            _LOGGER.info(
                "BatteryOperation change: %s → %s",
                self._last_op,
                op,
            )
            self._last_op = op
            # TODO Etap 2D: self._tasks.run_background(self._applier.apply(op))

        return self._make_result(op)

    @callback
    def update_t1_reserve(self, catalog: TargetSocCatalog) -> None:
        """Refresh the T1 reserves from the latest forecast.

        Called on every energy-balance recalculation. Which variant and which
        part of the window feed the reserve depends on the time of day:

        - before 07:00 — today's LIVE_SOFT, whole window (the night charge
          target),
        - 07:00-13:00 — today's LIVE_SOFT, what is left of the window (falls),
        - 13:00-16:00 — today's LIVE_SOFT, whole window replayed after it
          closed,
        - from 16:00 — TOMORROW_LIVE_SOFT, whole window (evening preview; the
          night charges to it, and a restart after midnight still has it).

        A variant with nothing computed yet leaves the remembered value alone.
        The LIVE+EXTRAP reserve is in-memory only and exists until 16:00 —
        EXTRAP variants have no tomorrow.
        """
        now = self._clock()
        self._t1_reserve_extrap = self._resolve_extrap_reserve(now, catalog)
        reserve = self._resolve_t1_reserve(now, catalog)
        if reserve is None or not self._repo.schedule.set_t1_reserve(reserve, now):
            self._notify_all()
            return
        self._repo.save_if_changed()
        self._notify_all()

    @staticmethod
    def _resolve_t1_reserve(
        now: datetime, catalog: TargetSocCatalog
    ) -> T1SocReserve | None:
        if now.hour >= BatteryScheduleService._TOMORROW_FROM_HOUR:
            target_soc = catalog.target_socs.get(PvForecast.TOMORROW_LIVE_SOFT)
            if target_soc is None:
                return None
            return T1SocReserve.from_target_soc(
                now.date() + timedelta(days=1),
                target_soc,
                source="tomorrow_live_soft",
                window=ReserveWindow.FULL,
            )
        target_soc = catalog.target_socs.get(PvForecast.LIVE_SOFT)
        if target_soc is None:
            return None
        return T1SocReserve.from_target_soc(
            now.date(),
            target_soc,
            source="live_soft",
            window=BatteryScheduleService._today_window(now),
        )

    @staticmethod
    def _resolve_extrap_reserve(
        now: datetime, catalog: TargetSocCatalog
    ) -> T1SocReserve | None:
        """Highest reserve over today's variants except AT_6 (None from 16:00)."""
        if now.hour >= BatteryScheduleService._TOMORROW_FROM_HOUR:
            return None
        window = BatteryScheduleService._today_window(now)
        candidates = [
            reserve
            for variant in PvForecast.today()
            if variant is not PvForecast.AT_6
            and (target_soc := catalog.target_socs.get(variant)) is not None
            and (
                reserve := T1SocReserve.from_target_soc(
                    now.date(),
                    target_soc,
                    # Same naming as the sensor.rce_target_soc_<suffix> entities.
                    source=variant.pretty_label.lower().replace(" ", "_"),
                    window=window,
                )
            )
            is not None
        ]
        return max(candidates, key=lambda r: r.value) if candidates else None

    @staticmethod
    def _today_window(now: datetime) -> ReserveWindow:
        if now.hour < BatteryScheduleService._T1_START_HOUR:
            return ReserveWindow.FULL
        if now.hour < BatteryScheduleService._T1_END_HOUR:
            return ReserveWindow.REMAINING
        return ReserveWindow.PAST

    @property
    def t1_reserve(self) -> T1SocReserve | None:
        """Latest remembered LIVE_SOFT reserve (any day)."""
        return self._repo.schedule.t1_reserve

    @callback
    def set_morning_discharge(self, plan: MorningDischargePlan | None) -> None:
        """Take today's automatic morning discharge plan (in memory only)."""
        if self._repo.schedule.set_morning_discharge(plan):
            self._notify_all()

    @property
    def morning_discharge(self) -> MorningDischargePlan | None:
        """Today's automatic morning discharge plan, None when there is none."""
        return self._repo.schedule.morning_discharge_for(self._clock().date())

    @property
    def t1_reserve_extrap(self) -> T1SocReserve | None:
        """LIVE+EXTRAP reserve for today, None from 16:00."""
        return self._t1_reserve_extrap

    def _make_result(self, op: BatteryOperation) -> BatteryScheduleUpdateResult:
        """Capture current aggregate state alongside given operation."""
        return BatteryScheduleUpdateResult(
            operation=op,
            ems_interventions_blocked=self._repo.schedule.ems_interventions_blocked,
            ems_interventions_blocked_override=(
                self._repo.schedule.ems_interventions_blocked_override
            ),
            schedule_active_this_hour=self._repo.schedule.is_active_this_hour(
                self._clock()
            ),
        )

    # ─── Properties exposed to Ems + entities (avoid Ems leaking repo) ───

    @property
    def currently_engaging(self) -> str:
        """Name of currently engaged SlotKind, or 'IDLE' when no slot active.

        String form exposed for `sensor.ems_battery_schedule_currently_engaging`
        (Etap 2B observability) — sensor reads this directly. Underlying
        aggregate field is `SlotKind | None`; we stringify here so the sensor
        doesn't need to know about the enum.
        """
        slot = self._repo.schedule.currently_engaging
        return slot.name if slot is not None else "IDLE"

    @property
    def last_op(self) -> BatteryOperation:
        """Last computed BatteryOperation — exposed for cross-service seeding.

        Used by ems_factory to seed BatteryChargeService._last_schedule_op
        on startup so sensor reads immediately after reload reflect the
        persisted engagement instead of `idle()`.
        """
        return self._last_op

    @property
    def schedule_active_this_hour(self) -> bool:
        """True if engaged now OR disengaged within current clock hour."""
        return self._repo.schedule.is_active_this_hour(self._clock())

    @property
    def ems_interventions_blocked_override(self) -> bool:
        """User-controlled half of `ems_interventions_blocked` (no engagement part)."""
        return self._repo.schedule.ems_interventions_blocked_override

    # ─── User mutators ───

    async def set_ems_interventions_blocked_override(self, value: bool) -> None:
        """UI-driven async toggle of the user-controlled override flag.

        Mutates `BatterySchedule._interventions_blocked_override`, persists
        via repo (awaited), and notifies listeners on actual delta. Schedule
        engagement is managed separately by `compute_operation` — this
        method only controls the user-facing portion of the combined
        `ems_interventions_blocked` derived property.
        """
        await self._persist_and_notify(
            self._repo.schedule.set_ems_interventions_blocked_override(value)
        )

    # ─── Slot read/write (Etap 2C) ───

    def slot(self, scope: Scope, kind: SlotKind) -> BatteryScheduleEntry:
        """Return <scope>_<kind> entry — read-side for UI entities."""
        if scope == "today":
            return self._repo.schedule.today_entry_for(kind)
        return self._repo.schedule.tomorrow_entry_for(kind)

    async def handle_slot_command(self, cmd: SlotCommand) -> None:
        """Apply a slot Command to the aggregate. Persists + notifies on delta.

        Entities (switch/time/number) construct a typed Command (e.g.
        `SetSlotEnabledCommand`) and forward it through this method. The
        aggregate owns the read-modify-write lifecycle via
        `apply_slot_command`; the Command owns the transformation
        (`apply_to_entry`). Adding a new editable field = new Command class
        with no change to the service.

        `BatteryScheduleEntry.__post_init__` validates invariants and raises
        ValueError on bad input — propagates to the entity callback (HA
        renders as service call failure; UI restricts ranges, defense in depth).
        """
        await self._persist_and_notify(
            self._repo.schedule.apply_manual_slot_command(
                cmd, today=self._edited_evening()
            )
        )

    def _edited_evening(self) -> date:
        """Which evening a hand edit applies to — the same one a run would plan.

        The slots are a single set reused every day, so an edit made after the
        evening window has passed is aimed at tomorrow. Stamping it with the
        calendar date would mark the wrong evening as hand-set: the planner
        would then either ignore a deliberate change or defer to a spent one.
        """
        now = self._clock()
        if EveningPlan.plans_tomorrow_at(now, is_workday=self._is_workday_today()):
            return now.date() + timedelta(days=1)
        return now.date()

    def _is_workday_today(self) -> bool:
        """Today's calendar from the last tick; assumes workday when unknown.

        A workday's window ends earlier, so assuming one moves the switchover
        earlier too — which at worst stamps an edit with tomorrow when today
        still had an hour left, rather than the other way round.
        """
        return self._last_is_workday is not False

    @property
    def evening_is_hand_set_today(self) -> bool:
        """Tell whether the user has overridden an evening slot today."""
        return self._repo.schedule.evening_is_hand_set_on(self._clock().date())

    @property
    def is_discharging_now(self) -> bool:
        """Tell whether a discharge slot is engaged right now.

        The evening run waits for this to clear: rewriting a slot mid-discharge
        would cut it short.
        """
        kind = self._repo.schedule.currently_engaging
        return kind is not None and kind.direction.is_discharge

    async def adopt_evening_plan(
        self, plan: EveningPlan, now: datetime, *, force: bool = False
    ) -> PlanApplication:
        """Write the evening plan into the slots and say what came of it.

        Stands down while the user has edited an evening slot by hand today —
        a deliberate change outranks a scheduled run. Two things override it:

        - `force`, used by the dashboard button, because pressing
          "recalculate" is an explicit request and would otherwise do nothing
          exactly when there is most reason to press it;
        - a plan emptied by tomorrow morning's prices, which is information
          the user did not have when they edited.

        Both hand the evening back to automatic control afterwards.

        Returns the outcome plus a slot-by-slot diff rather than a bool:
        "nothing changed", "I kept my hands off your settings" and "I moved
        these two windows" are three different messages to whoever reads them.
        """
        schedule = self._repo.schedule
        may_override = force or plan.overruled_by_morning
        # Against the PLANNED day, not the current one: the evening run fires
        # at 22:05 for tomorrow, by which time an edit made for tonight is
        # spent. Asking about today there would let one manual tweak block the
        # next day's planning entirely.
        if schedule.evening_is_hand_set_on(plan.day) and not may_override:
            _LOGGER.debug("Evening plan skipped — hand-set on %s", plan.day)
            return PlanApplication(outcome=PlanOutcome.DEFERRED_TO_MANUAL)
        before = self._evening_snapshot()
        changed = False
        for cmd in plan.slot_commands():
            changed |= schedule.apply_slot_command(cmd)
        if may_override:
            changed |= schedule.release_evening_to_proposer()
        await self._persist_and_notify(changed)
        changes = self._diff_against(before)
        return PlanApplication(
            outcome=PlanOutcome.APPLIED if changes else PlanOutcome.ALREADY_CURRENT,
            changes=changes,
        )

    def _evening_snapshot(self) -> dict[SlotKind, BatteryScheduleEntry]:
        """Capture the evening entries, for diffing once the plan lands."""
        return {kind: self._repo.schedule.today_entry_for(kind) for kind in _EVENING}

    def _diff_against(
        self, before: dict[SlotKind, BatteryScheduleEntry]
    ) -> tuple[SlotChange, ...]:
        """Evening slots whose settings the plan actually moved."""
        return tuple(
            SlotChange(kind=kind, before=was, after=now_entry)
            for kind, was in before.items()
            if (now_entry := self._repo.schedule.today_entry_for(kind)) != was
        )

    # ─── One-shot (Etap 2F) ───

    @property
    def oneshot(self) -> OneShotOperation | None:
        """Active one-shot operation, or None when idle."""
        return self._repo.schedule.oneshot

    def oneshot_params(self, direction: Direction) -> OneShotParams:
        """User-editable one-shot defaults for the given direction."""
        return self._repo.schedule.oneshot_params(direction)

    async def handle_start_oneshot(self, direction: Direction) -> None:
        """Start a one-shot operation in the given direction. Persists + notifies."""
        events = self._repo.schedule.start_oneshot(direction, self._clock())
        if not events:
            return
        await self._repo.persist()
        for event in events:
            _LOGGER.info("BatteryScheduleEvent: %s", event)
            if self._notifier is not None:
                self._notifier.notify(event)
        self._notify_all()

    async def handle_cancel_oneshot(self) -> None:
        """Cancel active one-shot. Persists + notifies if was active."""
        events = self._repo.schedule.cancel_oneshot(self._clock())
        if not events:
            return
        await self._repo.persist()
        for event in events:
            _LOGGER.info("BatteryScheduleEvent: %s", event)
            if self._notifier is not None:
                self._notifier.notify(event)
        self._notify_all()

    async def handle_oneshot_command(self, cmd: OneShotParamsCommand) -> None:
        """Apply one-shot params Command. Persists + notifies on delta.

        `OneShotParams.__post_init__` validates target_soc range and raises
        ValueError on bad input — propagates to entity callback.
        """
        await self._persist_and_notify(self._repo.schedule.apply_oneshot_command(cmd))
