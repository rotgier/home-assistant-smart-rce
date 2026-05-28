"""BatteryChargeCurrentActuator — driven adapter for Modbus battery_charge_current.

Goodwe HA integration doesn't expose an entity for `battery_charge_current`
(Modbus register 45353, Kind.BAT) — only `goodwe.set_parameter` /
`goodwe.get_parameter` services. The `get_parameter` service additionally
requires an `entity_id` write target and discards the return value, which
would force us to depend on an external `input_number` helper as a
readback buffer.

We bypass the service layer and call `Inverter.read_setting()` /
`Inverter.write_setting()` directly via `hass.data[goodwe.DOMAIN]`. This
is the public API of the `goodwe` Python library (the HA integration only
wraps it) so the coupling is minimal — we depend on:
- `hass.data["goodwe"]` registry shape (HA convention, stable in fork)
- `runtime_data.inverter` field name and `.device_info["identifiers"]`
  (matches `goodwe/services.py:_get_inverter_by_device_id`)
- `Inverter.read_setting(name)` / `.write_setting(name, value)` (goodwe
  lib public API)

Plus: no external `input_number` helper needed; smart_rce fully owns
the Modbus readback pipeline. `sensor.ems_battery_charge_current` is the
single user-visible display of the cached readback (`policy._modbus_current_value`).

State-diff: target (`policy.target_modbus_value`, derived) vs cached
`policy.modbus_current_value`. Only writes when delta detected.

Drift detection: periodic refresh every 5 minutes via
`async_track_time_interval` — catches "ktoś klikał scene.apply" or other
external interference.

Restart safety: `_modbus_current_value` persisted in
`BatteryChargeRepository`; on `EVENT_HOMEASSISTANT_STARTED` we run a
one-shot reconcile to sync cache with actual Modbus state before any
writes fire.

Hexagonal pattern: driven adapter (outbound). Depends on Repository only —
no Service back-reference. `apply_if_changed(schedule_op, now)` invoked
from `BatteryChargeService.update`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .async_task_runner import AsyncTaskRunner

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from ..domain.battery_schedule import BatteryOperation
    from .battery_charge_repository import BatteryChargeRepository

_LOGGER = logging.getLogger(__name__)

# Goodwe integration domain + our inverter device id (matches other actuators).
GOODWE_DOMAIN = "goodwe"
GOODWE_DEVICE_ID = "690e4551a45b55c24447b0ae3c05942c"

# Modbus parameter name (per goodwe lib `et.py` register 45353).
PARAMETER = "battery_charge_current"

# Modbus write → readback delay. `inverter.write_setting` returns when the
# write completes, but the inverter takes some time to settle internally
# before a subsequent read returns the new value. 5s is the legacy timing
# from YAML adapter automations 172-218 (we keep it for safety).
WRITE_TO_READBACK_DELAY_SEC = 5

# Periodic drift refresh interval.
PERIODIC_REFRESH = timedelta(minutes=5)


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
        entry: ConfigEntry,
        repo: BatteryChargeRepository,
        tasks: AsyncTaskRunner,
        *,
        writes_enabled: bool = False,
    ) -> None:
        """Self-wires lifecycle on construction (Java @PostConstruct analog).

        All listeners registered here are sync (no await), so the constructor
        is the natural place to wire them. Factory just calls
        `BatteryChargeCurrentActuator(hass, entry, repo, tasks)` — no
        second `schedule_periodic_refresh(entry)` step to forget.

        - 5-min drift refresh via `async_track_time_interval`
        - Startup reconcile on `EVENT_HOMEASSISTANT_STARTED` (or immediate
          if HA is already running — config_entry reload scenario) so the
          Goodwe integration is loaded before we call `inverter.read_setting`.
          Pattern matches state_mapper + weather_listener.
        """
        self._hass = hass
        self._repo = repo
        self._tasks = tasks
        self._lock = asyncio.Lock()
        self._writes_enabled = writes_enabled

        entry.async_on_unload(
            async_track_time_interval(hass, self._on_periodic_tick, PERIODIC_REFRESH)
        )
        if hass.state == CoreState.running:
            # HA already up (reload scenario) — fire reconcile directly. We're
            # in sync __init__ context so the async _on_ha_started must be
            # spawned as a task.
            tasks.run_background(
                self._on_ha_started(None),
                name="smart_rce_battery_charge_startup_reconcile",
            )
        else:
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._on_ha_started)

    # ─── lifecycle callbacks (async — HA awaits via async_run_hass_job) ───

    async def _on_periodic_tick(self, _now: datetime) -> None:
        """5-min drift refresh — refresh cache, log warning if diverged."""
        async with self._lock:
            cached = self._repo.policy.modbus_current_value
            readback = await self._refresh_modbus_cache()
            if readback is None:
                return
            if cached is not None and cached != readback:
                _LOGGER.warning(
                    "BatteryChargeCurrentActuator: drift detected — "
                    "cached=%.1f, readback=%.1f",
                    cached,
                    readback,
                )

    async def _on_ha_started(self, _event: Event) -> None:
        """One-shot Modbus cache reconcile after HA fully started."""
        async with self._lock:
            await self._refresh_modbus_cache()

    # ─── per-tick state-diff dispatch (called from BatteryChargeService.update) ───

    @callback
    def apply_if_changed(self, schedule_op: BatteryOperation, now: datetime) -> None:
        """State-diff trigger — called from Ems.update_state every tick.

        Compares target (derived) vs cached Modbus value. If equal: skip
        (no Modbus traffic). If different: spawn background apply task.

        Skips when cache is None — goodwe integration not yet loaded
        (typical at HA startup) or recent reads failed. Startup reconcile
        (EVENT_HOMEASSISTANT_STARTED) + periodic refresh (5 min) populate
        the cache; without this guard every tick during the boot window
        would attempt write_setting → exception → log noise.

        No-op when `writes_enabled=False` (two-stage deploy safety).
        """
        if not self._writes_enabled:
            return
        current = self._repo.policy.modbus_current_value
        if current is None:
            _LOGGER.debug(
                "BatteryChargeCurrentActuator: skipping — Modbus cache unavailable"
            )
            return
        target = self._repo.policy.target_modbus_value(now, schedule_op)
        if target == current:
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

    async def _set_parameter(self, value: float) -> None:
        """Write Modbus parameter via Inverter.write_setting (goodwe lib API)."""
        inverter = self._get_inverter()
        if inverter is None:
            raise RuntimeError("goodwe inverter not loaded")
        await inverter.write_setting(PARAMETER, value)

    # ─── common helpers — multi-caller (_on_periodic_tick, _on_ha_started, _dispatch_apply) ───

    async def _refresh_modbus_cache(self) -> float | None:
        """Read Modbus parameter via Inverter.read_setting → write to policy.

        Returns the read value (or None if not available — goodwe not yet
        loaded, Modbus failure, parse error). Caller must hold the lock.
        """
        inverter = self._get_inverter()
        if inverter is None:
            _LOGGER.debug(
                "BatteryChargeCurrentActuator: goodwe inverter not loaded — skipping read"
            )
            return None
        try:
            raw = await inverter.read_setting(PARAMETER)
        except Exception:
            _LOGGER.exception("BatteryChargeCurrentActuator: read_setting failed")
            return None
        try:
            value = float(raw)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "BatteryChargeCurrentActuator: read_setting returned non-numeric %r",
                raw,
            )
            return None
        await self._repo.record_modbus_read(value, dt_util.now())
        return value

    def _get_inverter(self):
        """Resolve `Inverter` instance from `hass.data["goodwe"]` runtime_data.

        Mirrors `goodwe/services.py:_get_inverter_by_device_id`: looks up
        the device by id, then matches against `runtime_data.device_info`
        identifiers across all goodwe config entries. Returns None if goodwe
        isn't loaded yet (typical at HA startup before
        EVENT_HOMEASSISTANT_STARTED).
        """
        runtime_datas = self._hass.data.get(GOODWE_DOMAIN)
        if not runtime_datas:
            return None
        device = dr.async_get(self._hass).async_get(GOODWE_DEVICE_ID)
        if device is None:
            return None
        for runtime_data in runtime_datas.values():
            if device.identifiers == runtime_data.device_info.get("identifiers"):
                return runtime_data.inverter
        return None
