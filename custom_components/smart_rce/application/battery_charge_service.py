"""BatteryChargeService — application orchestrator + use case facade.

Public API consumed by HA entities (select, sensors) and Ems:
- `update(schedule_op)` — per-tick orchestration; caches latest
  `BatteryOperation` so derived properties (`charge_allowed`,
  `target_modbus_value`) can be queried lazily by sensors/actuator.
- `override_mode` / `set_override_mode(mode)` — select entity bridge
- `charge_allowed` / `modbus_current_value` — sensor/actuator queries
- `add_override_listener(cb)` — UI refresh after user toggle

Repository is the internal collaborator (owns + persists policy). Service
does NOT track `_last_charge_allowed` shadow state — the actuator's
state-diff (target vs Modbus readback cache) is the single source for
write triggers. Sensors fire `async_write_ha_state` from the Ems-level
listener and HA core dedupes on attribute equality.

DDD application layer:
- HASS-unaware (no `hass`, no HA service calls)
- Dependencies injected via constructor (repo + clock)
- Use case methods compose domain mutations + repo persistence + listener
  notifications explicitly
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
from dataclasses import dataclass
from datetime import datetime, time
import logging
from typing import TYPE_CHECKING

from homeassistant.core import callback

from ..domain.battery_charge_policy import OverrideMode
from ..domain.battery_schedule import BatteryOperation
from ..domain.charge_slots import StartChargeTodayChanged


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


if TYPE_CHECKING:
    from ..infrastructure.battery_charge_current_actuator import (
        BatteryChargeCurrentActuator,
    )
    from ..infrastructure.battery_charge_repository import BatteryChargeRepository

_LOGGER = logging.getLogger(__name__)


class BatteryChargeService:
    """Application service. HASS-unaware — dependencies injected at construction."""

    def __init__(
        self,
        repo: BatteryChargeRepository,
        clock: Callable[[], datetime],
        actuator: BatteryChargeCurrentActuator,
    ) -> None:
        self._repo = repo
        self._clock = clock
        self._actuator = actuator
        self._last_schedule_op: BatteryOperation = BatteryOperation.idle()
        self._override_listeners: list[Callable[[OverrideMode], None]] = []
        self._start_charge_hour_listeners: list[Callable[[time | None], None]] = []

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
    def target_modbus_value(self) -> float:
        return self._repo.policy.target_modbus_value(
            self._clock(), self._last_schedule_op
        )

    @property
    def modbus_current_value(self) -> float | None:
        return self._repo.policy.modbus_current_value

    @property
    def last_modbus_read_at(self) -> datetime | None:
        return self._repo.policy.last_modbus_read_at

    @property
    def override_mode(self) -> OverrideMode:
        return self._repo.policy.user_override_mode

    @property
    def start_charge_hour_override(self) -> time | None:
        return self._repo.policy.start_charge_hour_override

    # ─── User override — public mutators ───

    async def set_override_mode(self, mode: OverrideMode) -> None:
        """UI-driven select option change. Persists + notifies listeners on delta."""
        previous = self._repo.policy.user_override_mode
        if previous == mode:
            return
        await self._repo.set_override_mode(mode)
        _LOGGER.info(
            "BatteryChargeService: override_mode %s → %s",
            previous.value,
            mode.value,
        )
        self._notify_override_listeners(mode)

    def add_override_listener(
        self, cb: Callable[[OverrideMode], None]
    ) -> Callable[[], None]:
        """Subscribe to override-mode changes. Returns unsubscribe callable."""
        self._override_listeners.append(cb)

        def _unsub() -> None:
            with contextlib.suppress(ValueError):
                self._override_listeners.remove(cb)

        return _unsub

    def _notify_override_listeners(self, mode: OverrideMode) -> None:
        for cb in self._override_listeners:
            cb(mode)

    # ─── start_charge_hour_override — public mutators ───

    async def set_start_charge_hour_override(self, value: time | None) -> None:
        """UI-driven time entity change. Persists + notifies listeners on delta."""
        previous = self._repo.policy.start_charge_hour_override
        if previous == value:
            return
        await self._repo.set_start_charge_hour_override(value)
        _LOGGER.info(
            "BatteryChargeService: start_charge_hour_override %s → %s",
            previous,
            value,
        )
        self._notify_start_charge_hour_listeners(value)

    def add_start_charge_hour_override_listener(
        self, cb: Callable[[time | None], None]
    ) -> Callable[[], None]:
        """Subscribe to start_charge_hour_override changes."""
        self._start_charge_hour_listeners.append(cb)

        def _unsub() -> None:
            with contextlib.suppress(ValueError):
                self._start_charge_hour_listeners.remove(cb)

        return _unsub

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
        value = event.new_value
        if previous == value:
            return
        self._repo.policy.set_start_charge_hour_override(value)
        self._repo.save_if_changed()
        _LOGGER.info(
            "BatteryChargeService: auto-sync start_charge_hour_override %s → %s",
            previous,
            value,
        )
        self._notify_start_charge_hour_listeners(value)

    def _notify_start_charge_hour_listeners(self, value: time | None) -> None:
        for cb in self._start_charge_hour_listeners:
            cb(value)
