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
from datetime import datetime
import logging
from typing import TYPE_CHECKING

from homeassistant.core import callback

from ..domain.battery_schedule import (
    BatteryOperation,
    BatteryScheduleEntry,
    BatteryScheduleInput,
    Direction,
    OneShotOperation,
    OneShotParams,
    Scope,
    SetOneShotEndTimeCommand,
    SetOneShotTargetSocCommand,
    SlotCommand,
    SlotKind,
)
from ..infrastructure.battery_schedule_repository import BatteryScheduleRepository
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
    schedule_active_this_hour: bool


if TYPE_CHECKING:
    from ..infrastructure.async_task_runner import AsyncTaskRunner
    from ..infrastructure.battery_schedule_notifier import BatteryScheduleNotifier

_LOGGER = logging.getLogger(__name__)


class BatteryScheduleService(Service[BatteryScheduleRepository]):
    """Application service. HASS-unaware — dependencies injected at construction.

    Use case methods (set_ems_interventions_blocked_override, add_listener)
    are the external API for HA entities. Repository stays internal.
    """

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
        # Track last BatteryOperation to skip no-op applies (Etap 2D applier will
        # use this to avoid spurious Goodwe writes when the op didn't change).
        #
        # Reconstruct from persisted schedule state so the first post-reload
        # tick (which may have battery_soc=None during HA state cache warm-up)
        # returns the slot that was engaged pre-reload rather than `idle()`.
        # Without this, a long-running CHARGE/DISCHARGE slot would briefly
        # report `idle` to downstream consumers (e.g. BatteryChargeService
        # would flip `needs_charge_toggle` False → write Modbus → flicker).
        engaging = self._repo.schedule.currently_engaging
        if engaging is not None:
            entry = self._repo.schedule.today_entry_for(engaging)
            self._last_op: BatteryOperation = BatteryOperation.from_entry(entry)
        else:
            self._last_op = BatteryOperation.idle()

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
        if input.battery_soc is None:
            return self._make_result(self._last_op)

        # ─── Domain logic — aggregate decides operation + emits events ───
        op, events = self._repo.schedule.compute_operation(
            self._clock(), input.battery_soc
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

    def _make_result(self, op: BatteryOperation) -> BatteryScheduleUpdateResult:
        """Capture current aggregate state alongside given operation."""
        return BatteryScheduleUpdateResult(
            operation=op,
            ems_interventions_blocked=self._repo.schedule.ems_interventions_blocked,
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
        await self._persist_and_notify(self._repo.schedule.apply_slot_command(cmd))

    # ─── One-shot (Etap 2F) ───

    @property
    def oneshot(self) -> OneShotOperation | None:
        """Active one-shot operation, or None when idle."""
        return self._repo.schedule.oneshot

    def oneshot_params(self, direction: Direction) -> OneShotParams:
        """User-editable one-shot defaults for the given direction."""
        if direction.is_discharge:
            return self._repo.schedule.discharge_oneshot_params
        return self._repo.schedule.charge_oneshot_params

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

    async def handle_oneshot_command(
        self, cmd: SetOneShotTargetSocCommand | SetOneShotEndTimeCommand
    ) -> None:
        """Apply one-shot params Command. Persists + notifies on delta.

        `OneShotParams.__post_init__` validates target_soc range and raises
        ValueError on bad input — propagates to entity callback.
        """
        await self._persist_and_notify(self._repo.schedule.apply_oneshot_command(cmd))
