"""BatteryScheduleService — application orchestrator + use case facade.

Public API consumed by HA entities (switch, dashboard cards) and Ems:
- `update(input)` — per-tick orchestration: calls aggregate compute_operation,
  persists changed aggregate state, notifies listeners when domain events
  fired (preemptive — gotowe na Etap 2B observability sensors).
- `user_override_active` — current user override state (property)
- `set_user_override(value)` — UI-driven async toggle (switch entity)
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

from ..domain.battery_schedule import BatteryOperation, BatteryScheduleInput
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
    from ..infrastructure.battery_schedule_repository import BatteryScheduleRepository

_LOGGER = logging.getLogger(__name__)


class BatteryScheduleService(Service["BatteryScheduleRepository"]):
    """Application service. HASS-unaware — dependencies injected at construction.

    Use case methods (set_user_override, add_listener) are the external API
    for HA entities. Repository stays internal.
    """

    def __init__(
        self,
        repo: BatteryScheduleRepository,
        clock: Callable[[], datetime],
        tasks: AsyncTaskRunner,  # noqa: ARG002 — kept for future applier wiring
    ) -> None:
        super().__init__(repo)
        self._clock = clock
        # Track last BatteryOperation to skip no-op applies (Etap 2D applier will
        # use this to avoid spurious Goodwe writes when the op didn't change).
        self._last_op: BatteryOperation = BatteryOperation.idle()

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

        # ─── Events — log + notify subscribers on engagement state change ───
        for event in events:
            _LOGGER.info("BatteryScheduleEvent: %s", event)
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
    def ems_interventions_blocked(self) -> bool:
        """Combined user-override OR active engagement — read by DodPolicy / GridExport."""
        return self._repo.schedule.ems_interventions_blocked

    @property
    def schedule_active_this_hour(self) -> bool:
        """True if engaged now OR disengaged within current clock hour."""
        return self._repo.schedule.is_active_this_hour(self._clock())

    @property
    def user_override_active(self) -> bool:
        """Current user-controlled override state (without schedule-engagement part)."""
        return self._repo.schedule._interventions_blocked_override  # noqa: SLF001

    # ─── User mutators ───

    async def set_user_override(self, value: bool) -> None:
        """UI-driven async toggle of the user-controlled part of override.

        Mutates `BatterySchedule._interventions_blocked_override`, persists
        via repo (awaited), and notifies listeners on actual delta. Schedule
        engagement is managed separately by `compute_operation` — this
        method only controls the user-facing portion of
        `ems_interventions_blocked`.
        """
        await self._persist_and_notify(
            self._repo.schedule.set_interventions_blocked_override(value)
        )
