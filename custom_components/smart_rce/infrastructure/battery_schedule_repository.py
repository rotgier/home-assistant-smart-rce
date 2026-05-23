"""BatteryScheduleRepository — driven adapter for BatterySchedule persistence.

Extends `Repository[BatterySchedule]`. Pure persistence: load aggregate
from `.storage/ems_battery_schedule`, save when dict differs from last save.
No business operations, no listeners — those live in `BatteryScheduleService`
(application layer).

Two-phase init:
1. `__init__(hass, tasks)` — constructs default aggregate + Store
2. `await repo.async_restore()` — loads persisted state if present
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from ..domain.battery_schedule import BatterySchedule
from .async_task_runner import AsyncTaskRunner
from .repository import Repository

_LOGGER = logging.getLogger(__name__)


class BatteryScheduleRepository(Repository[BatterySchedule]):
    """Persists BatterySchedule via HA Store. Pure persistence — no business logic."""

    STORAGE_KEY = "ems_battery_schedule"

    def __init__(self, hass: HomeAssistant, tasks: AsyncTaskRunner) -> None:
        super().__init__(hass, tasks)
        # Default empty aggregate — replaced by async_restore if persisted state exists.
        self._schedule: BatterySchedule = BatterySchedule()

    @property
    def schedule(self) -> BatterySchedule:
        return self._schedule

    def _get_aggregate(self) -> BatterySchedule:
        return self._schedule

    async def async_restore(self) -> None:
        """Call ONCE before Ems first tick. Replaces default schedule with persisted."""
        data: dict[str, Any] | None = await self._store.async_load()
        if data is not None:
            self._schedule = BatterySchedule.from_dict(data)
            self._last_saved = data
            _LOGGER.debug(
                "BatteryScheduleRepository: restored from %s "
                "(interventions_blocked=%s, engaging=%s)",
                self.STORAGE_KEY,
                self._schedule.ems_interventions_blocked,
                self._schedule._currently_engaging,  # noqa: SLF001
            )
