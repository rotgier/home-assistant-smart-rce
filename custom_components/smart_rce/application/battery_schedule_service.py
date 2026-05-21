"""BatteryScheduleService — application orchestrator + use case facade.

Public API consumed by HA entities (switch, dashboard cards) and Ems:
- `update(input)` — per-tick orchestration (Etap 2A adds compute_operation
  + side effects; Etap 0 is no-op)
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

Etap 0 vs Etap 2A:
- Etap 0: only user-override path is wired (switch entity + persistence).
  `update()` is no-op — no schedule engagement logic yet.
- Etap 2A: `update()` calls `schedule.compute_operation` + dispatches domain
  events to notifier + applies BatteryOperation via applier.
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
from datetime import datetime
import logging
from typing import TYPE_CHECKING

from homeassistant.core import callback

from ..domain.battery_schedule import BatteryScheduleInput

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

    @callback
    def update(self, input: BatteryScheduleInput) -> None:
        """No-op in Etap 0. Etap 2A adds compute_operation + side effects."""

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
        (Etap 2A) — this method only controls the user-facing portion of
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
