"""BatteryChargeRepository — owns + persists BatteryChargePolicy.

Driven adapter for HA Store (extends `Repository[BatteryChargePolicy]`).
Service + Actuator depend on this repo to read policy + persist state —
they are siblings, no circular dependency.

Async mutators (`record_modbus_read`, `set_override_mode`,
`set_start_charge_hour_override`) auto-persist *immediately* via `await
self._persist()` so the actuator's drift-detection loop can rely on Modbus
state being on disk before the next refresh tick (ADR-018 ~1s crash safety).

Two-phase init:
1. `__init__(hass, tasks)` — constructs default policy + Store
2. `await repo.async_restore()` — loads persisted state if present
"""

from __future__ import annotations

from datetime import datetime, time
import logging
from typing import Any

from homeassistant.core import HomeAssistant

from ..domain.battery_charge_policy import BatteryChargePolicy, OverrideMode
from .async_task_runner import AsyncTaskRunner
from .repository import Repository

_LOGGER = logging.getLogger(__name__)


class BatteryChargeRepository(Repository[BatteryChargePolicy]):
    """Persists BatteryChargePolicy via HA Store. Owns the policy aggregate."""

    STORAGE_KEY = "ems_battery_charge"

    def __init__(self, hass: HomeAssistant, tasks: AsyncTaskRunner) -> None:
        super().__init__(hass, tasks)
        self._policy: BatteryChargePolicy = BatteryChargePolicy()

    @property
    def policy(self) -> BatteryChargePolicy:
        return self._policy

    def _get_aggregate(self) -> BatteryChargePolicy:
        return self._policy

    async def async_restore(self) -> None:
        """Call ONCE before Ems first tick. Replaces default policy with persisted."""
        data: dict[str, Any] | None = await self._store.async_load()
        if data is not None:
            self._policy = BatteryChargePolicy.from_dict(data)
            self._last_saved = data
            _LOGGER.debug(
                "BatteryChargeRepository: restored from %s "
                "(override=%s, modbus_value=%s, last_read=%s)",
                self.STORAGE_KEY,
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
            await self._persist()

    async def set_override_mode(self, mode: OverrideMode) -> None:
        """Set user override + auto-persist on change."""
        if self._policy.set_user_override_mode(mode):
            await self._persist()

    async def set_start_charge_hour_override(self, value: time | None) -> None:
        """Set morning charge window start + auto-persist on change."""
        if self._policy.set_start_charge_hour_override(value):
            await self._persist()
