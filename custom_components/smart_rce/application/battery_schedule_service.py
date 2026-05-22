"""BatteryScheduleService — application orchestrator + use case facade.

Public API consumed by HA entities (switch, dashboard cards) and Ems:
- `update(input)` — per-tick orchestration: calls aggregate compute_operation,
  persists changed aggregate state. Etap 2D will add applier (HA service calls
  to inverter) and notifier (telegram via script.notify_alert) dispatch.
- `user_override_active` — current user override state (property)
- `set_user_override(value)` — UI-driven toggle (switch entity)
- `add_user_override_listener(cb)` — subscribe to user override changes

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
import contextlib
from datetime import datetime
import logging
from typing import TYPE_CHECKING

from homeassistant.core import callback

from ..domain.battery_schedule import BatteryOperation, BatteryScheduleInput

if TYPE_CHECKING:
    from ..infrastructure.async_task_runner import AsyncTaskRunner
    from ..infrastructure.battery_schedule_repository import BatteryScheduleRepository

_LOGGER = logging.getLogger(__name__)


class BatteryScheduleService:
    """Application service. HASS-unaware — dependencies injected at construction.

    Use case methods (set_user_override, add_user_override_listener) are the
    external API for HA entities. Repository stays internal.
    """

    def __init__(
        self,
        repo: BatteryScheduleRepository,
        clock: Callable[[], datetime],
        tasks: AsyncTaskRunner,
    ) -> None:
        self._repo = repo
        self._clock = clock
        self._tasks = tasks
        self._user_override_listeners: list[Callable[[bool], None]] = []
        self._last_user_override_state: bool = (
            self._repo.schedule._interventions_blocked_override  # noqa: SLF001
        )
        # Track last BatteryOperation to skip no-op applies (Etap 2D applier will
        # use this to avoid spurious Goodwe writes when the op didn't change).
        self._last_op: BatteryOperation = BatteryOperation.idle()

    @callback
    def update(self, input: BatteryScheduleInput) -> BatteryOperation:
        """Per-tick orchestration — called from Ems.update_state.

        Domain logic (compute_operation) runs synchronously inside the aggregate;
        side effects (persistence, applier, notifier) fire as fire-and-forget
        tasks. Etap 2A wires compute_operation + persistence; applier + notifier
        arrive in Etap 2D.

        Returns the freshly-computed BatteryOperation so downstream consumers
        (BatteryChargeService in Etap B, applier/notifier in Etap 2D) can
        pick it up without re-querying the aggregate.
        """
        if input.battery_soc is None:
            return self._last_op

        # ─── Domain logic — aggregate decides operation + emits events ───
        op, events = self._repo.schedule.compute_operation(
            self._clock(), input.battery_soc
        )

        # ─── Persistence — repo save_if_changed checks dict equality internally ───
        self._repo.save_if_changed()

        # ─── Events — log only in Etap 2A; notifier dispatch comes in Etap 2D ───
        for event in events:
            _LOGGER.info("BatteryScheduleEvent: %s", event)

        # ─── Apply — only when op changed; applier wired in Etap 2D ───
        if op != self._last_op:
            _LOGGER.info(
                "BatteryOperation change: %s → %s",
                self._last_op,
                op,
            )
            self._last_op = op
            # TODO Etap 2D: self._tasks.run_background(self._applier.apply(op))

        return op

    # ─────── User override — public methods called from HA entities ───────

    @property
    def user_override_active(self) -> bool:
        """Current user-controlled override state (without schedule-engagement part)."""
        return self._repo.schedule._interventions_blocked_override  # noqa: SLF001

    def set_user_override(self, value: bool) -> None:
        """UI-driven toggle of the user-controlled part of override.

        Mutates `BatterySchedule._interventions_blocked_override`, persists
        via repo (async fire-and-forget), and notifies listeners synchronously
        on actual value change (so switch UI refreshes without waiting for
        disk write to complete).

        Schedule engagement is managed separately by `compute_operation`
        — this method only controls the user-facing portion of
        `ems_interventions_blocked`.
        """
        if self.user_override_active == value:
            return
        self._repo.schedule._interventions_blocked_override = value  # noqa: SLF001
        self._repo.save_if_changed()
        if value != self._last_user_override_state:
            self._last_user_override_state = value
            for cb in self._user_override_listeners:
                cb(value)

    def add_user_override_listener(
        self, cb: Callable[[bool], None]
    ) -> Callable[[], None]:
        """Subscribe to user-override changes.

        Fires when `set_user_override` actually changes the value. Does NOT
        fire on schedule engagement changes — engagement affects the combined
        `ems_interventions_blocked` property but is a different concept than
        the user toggle. Returns unsubscribe callable.
        """
        self._user_override_listeners.append(cb)

        def _unsub() -> None:
            with contextlib.suppress(ValueError):
                self._user_override_listeners.remove(cb)

        return _unsub
