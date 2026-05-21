"""BatteryScheduleRepository — driven adapter for BatterySchedule persistence.

Pure persistence: load aggregate from `.storage/smart_rce_battery_schedule`,
save when dict differs from last save. No business operations, no listeners
— those live in `BatteryScheduleService` (application layer). Service uses
this repo, exposes use-case methods (`set_user_override`,
`add_user_override_listener`) externally.

Persistence pattern (consistent with `DodPolicyActuator.apply_if_changed`):
- `save_if_changed()` is a sync `@callback` — caller doesn't await. Internally
  fires a foreground task (`AsyncTaskRunner.run` — must complete before HA
  shutdown so `.storage/` write finalizes) executing `_persist()`.
- Idempotent: `_persist()` re-checks dict equality (so concurrent calls don't
  duplicate writes).

Two-phase init:
1. `__init__(store, tasks)` — constructs default aggregate
2. `await repo.async_restore()` — loads persisted state if present

Hexagonal pattern: **driven adapter (outbound)** — application service
dictates "save aggregate"; concrete impl uses HA Store. Wzór z
`dod_policy_repository.py`.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from homeassistant.core import callback
from homeassistant.helpers.storage import Store

from ..domain.battery_schedule import BatterySchedule
from .async_task_runner import AsyncTaskRunner

STORAGE_VERSION: Final[int] = 1
STORAGE_KEY: Final[str] = "smart_rce_battery_schedule"

_LOGGER = logging.getLogger(__name__)


class BatteryScheduleRepository:
    """Persists BatterySchedule via HA Store. Pure persistence — no business logic."""

    def __init__(self, store: Store[dict[str, Any]], tasks: AsyncTaskRunner) -> None:
        self._store = store
        self._tasks = tasks
        # Default empty aggregate — replaced by async_restore if persisted state exists.
        self._schedule: BatterySchedule = BatterySchedule()
        self._last_saved: dict[str, Any] | None = None

    @property
    def schedule(self) -> BatterySchedule:
        return self._schedule

    async def async_restore(self) -> None:
        """Call ONCE before Ems first tick. Replaces default schedule with persisted."""
        data = await self._store.async_load()
        if data is not None:
            self._schedule = BatterySchedule.from_dict(data)
            self._last_saved = data
            _LOGGER.debug(
                "BatteryScheduleRepository: restored from %s "
                "(interventions_blocked=%s, engaging=%s)",
                STORAGE_KEY,
                self._schedule.ems_interventions_blocked,
                self._schedule._currently_engaging,  # noqa: SLF001
            )

    @callback
    def save_if_changed(self) -> None:
        """Sync — fires foreground task to persist if aggregate changed."""
        self._tasks.run(self._persist(), name="smart_rce_battery_schedule_save")

    async def _persist(self) -> None:
        """Private — actual async save (called via tasks.run from save_if_changed)."""
        current = self._schedule.to_dict()
        if current == self._last_saved:
            return
        await self._store.async_save(current)
        self._last_saved = current
