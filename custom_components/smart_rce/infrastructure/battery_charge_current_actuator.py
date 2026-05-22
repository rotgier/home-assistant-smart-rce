"""BatteryChargeCurrentActuator — driven adapter for Modbus battery_charge_current.

Goodwe HA integration doesn't expose an entity for `battery_charge_current`
(Modbus register 45353, Kind.BAT) — only the `goodwe.set_parameter` /
`goodwe.get_parameter` services. This adapter wraps those services into an
idempotent "apply target value if different from cached readback" actuator.

Modbus readback channel:
- `goodwe.get_parameter` requires an `entity_id` to write the read value
  into. We reuse legacy `input_number.battery_charge_current` as the
  readback display channel (smart_rce-controlled after Etap B migration).
- After each write OR periodic refresh, we call `goodwe.get_parameter` →
  read `input_number.battery_charge_current` HA state → record into policy.

State-diff: target (`service.target_modbus_value`, derived from policy)
vs current (`service.modbus_current_value`, our cached Modbus readback).
Only writes when delta detected, avoiding spurious Modbus writes.

Drift detection: periodic refresh every 5 minutes via
`async_track_time_interval` — catches "ktoś klikał scene.apply" or other
external interference.

Restart safety: cached `_modbus_current_value` persisted in
`BatteryChargeRepository`; on restart we restore the cache, then schedule
a single delayed reconcile (30s post-startup so Goodwe integration is
loaded) to refresh the cache and write iff diverged from target.

Hexagonal pattern: driven adapter (outbound). Depends on Repository only —
no Service back-reference. `apply_if_changed(schedule_op, now)` invoked
explicitly from `Ems.update_state`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .async_task_runner import AsyncTaskRunner

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from ..domain.battery_schedule import BatteryOperation
    from .battery_charge_repository import BatteryChargeRepository

_LOGGER = logging.getLogger(__name__)

# Goodwe inverter device — same constant used by other actuators.
GOODWE_DEVICE_ID = "690e4551a45b55c24447b0ae3c05942c"

# Modbus parameter name (per goodwe lib `et.py` register 45353).
PARAMETER = "battery_charge_current"

# Modbus readback display channel (legacy `input_number` retained after
# Etap B migration — actuator-owned semantics, smart_rce updates it).
READBACK_ENTITY = "input_number.battery_charge_current"

# Modbus write → readback delay. `goodwe.set_parameter` is async and
# inverter takes some time to settle. 5s is the legacy timing from YAML
# adapter automations 172-218.
WRITE_TO_READBACK_DELAY_SEC = 5

# Periodic drift refresh interval.
PERIODIC_REFRESH = timedelta(minutes=5)

# Delay before initial post-startup reconcile (so Goodwe integration has
# time to finish loading and register its services).
STARTUP_RECONCILE_DELAY_SEC = 30


class BatteryChargeCurrentActuator:
    """Driven adapter — applies BatteryChargePolicy.target_modbus_value to Modbus.

    Two-stage deploy safety: actuator can be constructed with `writes_enabled=False`,
    in which case `apply_if_changed` is a no-op (write path), but read-side
    periodic refresh + startup reconcile still run (read Modbus into policy
    cache). This lets us deploy smart_rce code FIRST, observe sensor values,
    then deploy the coordinated YAML cleanup (delete adapter automations
    172-218 + repoint timer automations to new select) and re-enable writes
    in a follow-up bump. Without this flag the deploy window would race
    with legacy YAML adapter automations both writing Modbus.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        repo: BatteryChargeRepository,
        tasks: AsyncTaskRunner,
        *,
        writes_enabled: bool = False,
    ) -> None:
        self._hass = hass
        self._repo = repo
        self._tasks = tasks
        self._lock = asyncio.Lock()
        self._writes_enabled = writes_enabled

    def schedule_periodic_refresh(self, entry: ConfigEntry) -> None:
        """Register 5-min drift refresh + delayed startup reconcile.

        Called once from `ems_factory` after construction. Tied to entry
        lifecycle (unsubscribed on unload).
        """
        entry.async_on_unload(
            async_track_time_interval(
                self._hass, self._on_periodic_tick, PERIODIC_REFRESH
            )
        )
        # Delayed startup reconcile — Goodwe integration may not be ready
        # at smart_rce setup time. Fire-and-forget; if it fails (goodwe not
        # there yet) periodic refresh will catch up.
        self._tasks.run_background(
            self._delayed_startup_reconcile(),
            name="smart_rce_battery_charge_startup_reconcile",
        )

    @callback
    def apply_if_changed(self, schedule_op: BatteryOperation, now: datetime) -> None:
        """State-diff trigger — called from Ems.update_state every tick.

        Compares target (derived) vs cached Modbus value. If equal: skip
        (no Modbus traffic). If different: spawn background apply task.

        No-op when `writes_enabled=False` (two-stage deploy safety).
        """
        if not self._writes_enabled:
            return
        target = self._repo.policy.target_modbus_value(now, schedule_op)
        current = self._repo.policy.modbus_current_value
        if current is not None and target == current:
            return
        self._tasks.run_background(
            self._dispatch_apply(target),
            name="smart_rce_battery_charge_apply",
        )

    async def _dispatch_apply(self, target: float) -> None:
        """Write target to Modbus + read back into policy cache. Lock-protected."""
        async with self._lock:
            current = self._repo.policy.modbus_current_value
            if current is not None and target == current:
                return  # raced — apply_if_changed re-fired before this awaited
            previous_str = "unknown" if current is None else f"{current:.1f}"
            _LOGGER.info(
                "BatteryChargeCurrentActuator: %s → %.1f A", previous_str, target
            )
            try:
                await self._set_parameter(target)
            except Exception:
                _LOGGER.exception(
                    "BatteryChargeCurrentActuator: set_parameter failed (target=%.1f)",
                    target,
                )
                return
            await asyncio.sleep(WRITE_TO_READBACK_DELAY_SEC)
            await self._refresh_modbus_cache()

    @callback
    def _on_periodic_tick(self, _now: datetime) -> None:
        """5-min drift refresh — refresh cache, log warning if diverged."""
        self._tasks.run_background(
            self._periodic_refresh(),
            name="smart_rce_battery_charge_drift_refresh",
        )

    async def _periodic_refresh(self) -> None:
        async with self._lock:
            cached = self._repo.policy.modbus_current_value
            readback = await self._refresh_modbus_cache_inner()
            if readback is None:
                return
            if cached is not None and cached != readback:
                _LOGGER.warning(
                    "BatteryChargeCurrentActuator: drift detected — "
                    "cached=%.1f, readback=%.1f",
                    cached,
                    readback,
                )

    async def _delayed_startup_reconcile(self) -> None:
        """Wait for goodwe integration to load, then refresh Modbus cache once."""
        await asyncio.sleep(STARTUP_RECONCILE_DELAY_SEC)
        async with self._lock:
            await self._refresh_modbus_cache_inner()

    async def _refresh_modbus_cache(self) -> None:
        """Refresh Modbus cache (for call sites that already hold the lock)."""
        await self._refresh_modbus_cache_inner()

    async def _refresh_modbus_cache_inner(self) -> float | None:
        """Call goodwe.get_parameter → read input_number → write to policy.

        Returns the read value (or None if not available). Caller must hold
        the lock.
        """
        try:
            await self._hass.services.async_call(
                "goodwe",
                "get_parameter",
                {
                    "device_id": GOODWE_DEVICE_ID,
                    "parameter": PARAMETER,
                    "entity_id": READBACK_ENTITY,
                },
                blocking=True,
            )
        except Exception:
            _LOGGER.exception("BatteryChargeCurrentActuator: get_parameter failed")
            return None
        state = self._hass.states.get(READBACK_ENTITY)
        if state is None or state.state in ("unknown", "unavailable"):
            _LOGGER.debug(
                "BatteryChargeCurrentActuator: readback entity %s unavailable",
                READBACK_ENTITY,
            )
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "BatteryChargeCurrentActuator: invalid readback %r from %s",
                state.state,
                READBACK_ENTITY,
            )
            return None
        await self._repo.record_modbus_read(value, dt_util.now())
        return value

    async def _set_parameter(self, value: float) -> None:
        """Write Modbus parameter via goodwe.set_parameter."""
        await self._hass.services.async_call(
            "goodwe",
            "set_parameter",
            {
                "device_id": GOODWE_DEVICE_ID,
                "parameter": PARAMETER,
                "value": str(value),
            },
            blocking=True,
        )
