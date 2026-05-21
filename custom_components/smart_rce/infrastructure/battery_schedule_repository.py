"""BatteryScheduleRepository — driven adapter for BatterySchedule persistence.

Pure persistence: load aggregate from `.storage/smart_rce_battery_schedule`,
save when dict differs from last save. No business operations, no listeners
— those live in `BatteryScheduleService` (application layer). Service uses
this repo, exposes use-case methods (`set_user_override`,
`add_user_override_listener`) externally.

Two-phase init:
1. `__init__(store)` — constructs default aggregate
2. `await repo.async_restore()` — loads persisted state if present

Hexagonal pattern: **driven adapter (outbound)** — application service
dictates "save aggregate"; concrete impl uses HA Store. Wzór z
`dod_policy_persistence.py`.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from homeassistant.helpers.storage import Store

from ..domain.battery_schedule import BatterySchedule

STORAGE_VERSION: Final[int] = 1
STORAGE_KEY: Final[str] = "smart_rce_battery_schedule"

_LOGGER = logging.getLogger(__name__)


class BatteryScheduleRepository:
    """Persists BatterySchedule via HA Store. Pure persistence — no business logic."""

    def __init__(self, store: Store[dict[str, Any]]) -> None:
        self._store = store
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

    async def save_if_changed(self) -> None:
        """Persist aggregate if its serialized form differs from last save."""
        current = self._schedule.to_dict()
        if current == self._last_saved:
            return
        await self._store.async_save(current)
        self._last_saved = current
