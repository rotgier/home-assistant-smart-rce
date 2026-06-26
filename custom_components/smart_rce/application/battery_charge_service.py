"""BatteryChargeService — application orchestrator + use case facade.

Public API consumed by HA entities (select, time, sensors) and Ems:
- `update(schedule_op)` — per-tick orchestration; caches latest
  `BatteryOperation` so derived properties (`charge_allowed`,
  `target_modbus_value`) can be queried lazily by sensors/actuator.
  Returns `BatteryChargeUpdateResult` (atomic snapshot for Ems).
- `set_charge_allowed_override(mode)` / `set_start_charge_hour_override(value)`
  — UI mutators (async; persist + notify).
- `handle_start_charge_today_changed(event, now)` — sync event handler
  from `Ems.update_hourly` carrying a `ChargeSlots` rotation event;
  sticky-gates the auto-sync to bootstrap or `[00:00, 06:00)` window.
- `add_listener(cb)` — single-registry refresh hook (inherited from `Service`).

Repository is the internal collaborator (owns + persists policy). Service
does NOT track `_last_charge_allowed` shadow state — the actuator's
state-diff (target vs Modbus readback cache) is the single source for
write triggers. Sensors fire `async_write_ha_state` from the Ems-level
listener and HA core dedupes on attribute equality.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time
import logging
from typing import TYPE_CHECKING

from homeassistant.core import callback

from ..domain.battery_charge_policy import OverrideMode
from ..domain.battery_schedule import BatteryOperation
from ..domain.charge_slots import StartChargeTodayChanged
from ..infrastructure.battery_charge_repository import BatteryChargeRepository
from .service import Service

if TYPE_CHECKING:
    from ..infrastructure.battery_charge_current_actuator import (
        BatteryChargeCurrentActuator,
    )


@dataclass(frozen=True)
class BatteryChargeUpdateResult:
    """Return value of `BatteryChargeService.update`.

    Atomic capture of values Ems needs to pass to downstream managers
    (DodPolicy, GridExport) this tick — taken AFTER policy decisions
    settle. Lazy property reads on the service still work for external
    consumers (sensors).
    """

    charge_allowed: bool
    start_charge_hour_override: time | None


_LOGGER = logging.getLogger(__name__)


class BatteryChargeService(Service[BatteryChargeRepository]):
    """Application service. HASS-unaware — dependencies injected at construction."""

    def __init__(
        self,
        repo: BatteryChargeRepository,
        clock: Callable[[], datetime],
        actuator: BatteryChargeCurrentActuator,
        initial_schedule_op: BatteryOperation = BatteryOperation.idle(),
    ) -> None:
        super().__init__(repo)
        self._clock = clock
        self._actuator = actuator
        # `_last_schedule_op` is cached for sensor property reads (charge_allowed
        # below). Factory passes `initial_schedule_op` from BatteryScheduleService
        # so the first post-reload sensor read reflects the persisted engagement
        # (not `idle()` default) — matches the reconstruct-from-storage pattern
        # in BatteryScheduleService.__init__.
        self._last_schedule_op: BatteryOperation = initial_schedule_op

    @callback
    def update(self, schedule_op: BatteryOperation) -> BatteryChargeUpdateResult:
        """Per-tick hook called from Ems.update_state."""
        self._last_schedule_op = schedule_op
        self._actuator.apply_if_changed(schedule_op, self._clock())
        return BatteryChargeUpdateResult(
            charge_allowed=self._repo.policy.charge_allowed(self._clock(), schedule_op),
            start_charge_hour_override=self._repo.policy.start_charge_hour_override,
        )

    # ─── Properties (sensor / actuator queries) ───

    @property
    def charge_allowed(self) -> bool:
        return self._repo.policy.charge_allowed(self._clock(), self._last_schedule_op)

    @property
    def modbus_current_value(self) -> float | None:
        return self._repo.policy.modbus_current_value

    @property
    def charge_allowed_override(self) -> OverrideMode:
        return self._repo.policy.charge_allowed_override

    @property
    def start_charge_hour_override(self) -> time | None:
        return self._repo.policy.start_charge_hour_override

    @property
    def charge_hours_override(self) -> int | None:
        return self._repo.policy.charge_hours_override

    # ─── User mutators ───

    async def set_charge_allowed_override(self, mode: OverrideMode) -> None:
        """UI-driven select option change. Persists + notifies listeners on delta."""
        await self._persist_and_notify(
            self._repo.policy.set_charge_allowed_override(mode)
        )

    async def set_start_charge_hour_override(self, value: time | None) -> None:
        """UI-driven time entity change. Persists + notifies listeners on delta."""
        await self._persist_and_notify(
            self._repo.policy.set_start_charge_hour_override(value)
        )

    async def set_charge_hours_override(self, value: int | None) -> None:
        """UI-driven select change for charge-window length (None = Auto).

        Persists + notifies on delta. ChargeSlots recompute is driven by the
        caller (Ems) — this service owns only the persisted knob, not the
        ChargeSlots aggregate.
        """
        await self._persist_and_notify(
            self._repo.policy.set_charge_hours_override(value)
        )

    @callback
    def handle_start_charge_today_changed(
        self, event: StartChargeTodayChanged | None, now: datetime
    ) -> None:
        """React to ChargeSlots emitting a today-start change event.

        Sticky override gate — sync the new value into the policy only when:
        1. Bootstrap — policy.start_charge_hour_override is None (fresh install)
        2. Midnight window — `0 <= now.hour < 6`

        Outside these, user manual override on the time entity persists.
        Mirrors legacy YAML automation `copy-rce-start-charge-override-midnight`
        (gated by `condition: time after 00:00 before 06:00`).
        """
        if event is None:
            return
        previous = self._repo.policy.start_charge_hour_override
        if previous is not None and not (0 <= now.hour < 6):
            return  # sticky — user override survives outside midnight window
        self._save_if_changed_and_notify(
            self._repo.policy.set_start_charge_hour_override(event.new_value)
        )
