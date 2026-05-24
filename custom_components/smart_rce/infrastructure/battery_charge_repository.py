"""BatteryChargeRepository — owns + persists BatteryChargePolicy.

Driven adapter for HA Store (extends `Repository[BatteryChargePolicy]`).
Service + Actuator depend on this repo to read policy + persist state —
they are siblings, no circular dependency.

`record_modbus_read` is the only async mutator on the repository — it is
*actuator-facing*: the Modbus drift-detection loop reads back inverter
state every ~5s and the value must hit disk before the next refresh tick
(ADR-018 ~1s crash safety promise).

User-facing mutators (`set_charge_allowed_override`, `set_start_charge_hour_override`)
were dropped — the service mutates `policy.set_X(...)` directly and awaits
`repo.persist()` via the inherited `Service._persist_and_notify` helper.

Two-phase init:
1. `__init__(hass, tasks)` — constructs default policy + Store
2. `await repo.async_restore()` — loads persisted state if present
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.core import HomeAssistant

from ..domain.battery_charge_policy import BatteryChargePolicy
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
                self._policy.charge_allowed_override.value,
                self._policy.modbus_current_value,
                self._policy.last_modbus_read_at,
            )

    async def record_modbus_read(self, value: float, at: datetime) -> None:
        """Update Modbus cache + auto-persist on change.

        Actuator-facing: the drift-detection loop on the
        `BatteryChargeCurrentActuator` writes back observed Modbus state
        ~every 5s. Persistence must complete before the next refresh tick
        so a crash mid-loop does not lose the most recent observed value.

        Always updates `_last_modbus_read_at`; only persists if numeric value
        differs (avoids spurious disk writes during periodic drift checks).
        """
        if self._policy.record_modbus_read(value, at):
            await self.persist()
