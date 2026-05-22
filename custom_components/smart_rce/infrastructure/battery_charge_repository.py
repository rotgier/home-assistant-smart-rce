"""BatteryChargeRepository — owns + persists BatteryChargePolicy.

Driven adapter for HA Store. Repo *owns* the policy aggregate (analogous to
`BatteryScheduleRepository` which owns the `BatterySchedule` aggregate).
Service + Actuator depend on this repo to read policy + persist state —
they are siblings, no circular dependency.

Persistence pattern:
- `async record_modbus_read(value, at)` — actuator-facing async mutator.
  Returns nothing; if value changed, auto-persists via `_persist_now()`.
- `async set_override_mode(mode)` — service-facing async mutator. Same
  contract.
- `save_if_changed()` @callback — sync wrapper for sync callers (legacy
  parity with `BatteryScheduleRepository.save_if_changed`). Fires foreground
  task via `AsyncTaskRunner.run`.

The two async mutators auto-persist *immediately* (`_persist_now` awaits
the store write directly inside the call) so the actuator's drift-detection
loop can rely on Modbus state being on disk before the next refresh tick.
This matches ADR-018's ~1s crash safety promise.

Two-phase init:
1. `__init__(store, tasks)` — constructs default policy
2. `await repo.async_restore()` — loads persisted state if present
"""

from __future__ import annotations

from datetime import datetime, time
import logging
from typing import Any, Final

from homeassistant.core import callback
from homeassistant.helpers.storage import Store

from ..domain.battery_charge_policy import BatteryChargePolicy, OverrideMode
from .async_task_runner import AsyncTaskRunner

STORAGE_VERSION: Final[int] = 1
STORAGE_KEY: Final[str] = "ems_battery_charge"

_LOGGER = logging.getLogger(__name__)


class BatteryChargeRepository:
    """Persists BatteryChargePolicy via HA Store. Owns the policy aggregate."""

    def __init__(self, store: Store[dict[str, Any]], tasks: AsyncTaskRunner) -> None:
        self._store = store
        self._tasks = tasks
        self._policy: BatteryChargePolicy = BatteryChargePolicy()
        self._last_saved: dict[str, Any] | None = None

    @property
    def policy(self) -> BatteryChargePolicy:
        return self._policy

    async def async_restore(self) -> None:
        """Call ONCE before Ems first tick. Replaces default policy with persisted."""
        data = await self._store.async_load()
        if data is not None:
            self._policy = BatteryChargePolicy.from_dict(data)
            self._last_saved = data
            _LOGGER.debug(
                "BatteryChargeRepository: restored from %s "
                "(override=%s, modbus_value=%s, last_read=%s)",
                STORAGE_KEY,
                self._policy.user_override_mode.value,
                self._policy.modbus_current_value,
                self._policy.last_modbus_read_at,
            )

    async def record_modbus_read(self, value: float, at: datetime) -> None:
        """Update Modbus cache + auto-persist on change.

        Always updates `_last_modbus_read_at`; only persists if numeric value
        differs (avoids spurious disk writes during periodic drift checks).
        """
        if self._policy.record_modbus_read(value, at):
            await self._persist_now()

    async def set_override_mode(self, mode: OverrideMode) -> None:
        """Set user override + auto-persist on change."""
        if self._policy.set_user_override_mode(mode):
            await self._persist_now()

    async def set_start_charge_hour_override(self, value: time | None) -> None:
        """Set morning charge window start + auto-persist on change."""
        if self._policy.set_start_charge_hour_override(value):
            await self._persist_now()

    @callback
    def save_if_changed(self) -> None:
        """Sync wrapper for sync callers — fires foreground task via tasks.run."""
        self._tasks.run(self._persist_now(), name="smart_rce_battery_charge_save")

    async def _persist_now(self) -> None:
        """Write current policy state to Store. Idempotent: dict-equality guard."""
        current = self._policy.to_dict()
        if current == self._last_saved:
            return
        await self._store.async_save(current)
        self._last_saved = current
