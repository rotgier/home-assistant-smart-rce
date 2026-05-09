"""DodPolicyActuator — driven adapter for inverter DoD via scene.apply.

Apply `dod_policy.target_dod` to `number.goodwe_depth_of_discharge_on_grid`
as fire-and-forget background task. Mirrors `grid_export_actuator.py` pattern
(ADR-019) with two enhancements:

1. **Read-back verification**: after each scene.apply, immediately re-read
   inverter state. scene.apply blocking=True awaits Modbus write + state
   refresh, so post-write state SHOULD reflect new value. If not → silent
   failure → Telegram alert.

2. **No persisted `_last_applied`**: the inverter state itself is the source
   of truth. Each tick reads inverter state, compares with target, writes
   only if diverged. Self-healing across restarts and external drift (e.g.
   user manually changes DoD via UI, our next tick converges back to target).

3. **Logbook entries**: each successful apply emits a structured logbook
   entry (`name=DodPolicy`, `entity_id=number.goodwe_depth_of_discharge_on_grid`)
   so HA UI Logbook history shows what set the DoD register and why
   (target_dod + current phase).

Hexagonal pattern: **driven adapter (outbound)** — domain (DodPolicy)
dictates target value, concrete impl applies via HA `scene.apply` service.
"""

import asyncio
import logging

from homeassistant.components.logbook import async_log_entry
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..application.ems import Ems
from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

GOODWE_DOD_NUMBER = "number.goodwe_depth_of_discharge_on_grid"
NOTIFY_ALERT_SCRIPT = "script.notify_alert"


class DodPolicyActuator:
    """Driven adapter — applies DodPolicy.target_dod to inverter."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, ems: Ems) -> None:
        self._hass = hass
        self._entry = entry
        self._ems = ems
        self._lock = asyncio.Lock()

    @callback
    def apply_if_changed(self) -> None:
        """Spawn fire-and-forget background task (registered as ems listener)."""
        self._entry.async_create_background_task(
            self._hass,
            self._dispatch(),
            name="smart_rce_dod_apply",
        )

    async def _dispatch(self) -> None:
        async with self._lock:
            target = self._ems.dod_policy.target_dod
            current = self._read_inverter_dod()

            if current is None:
                # Goodwe integration not yet loaded (typical at HA startup —
                # smart_rce loads before goodwe). Skip without alert; next
                # ems tick will retry once goodwe entity is ready.
                _LOGGER.debug(
                    "DodPolicyActuator: skipping — inverter entity unavailable"
                )
                return

            if current == target:
                return  # Inverter already at target — no write needed

            try:
                await self._apply_scene(target)
            except Exception:
                _LOGGER.exception(
                    "DodPolicyActuator: scene.apply raised for target=%d", target
                )
                await self._notify_alert(target, current, reason="apply_exception")
                return

            # scene.apply blocking=True → after await, inverter state MUST reflect
            # the new value. If not, silent failure (Modbus reject, integration bug).
            post_write = self._read_inverter_dod()
            if post_write is None:
                # Defensive: state momentarily unavailable post-write. Don't
                # alert — next tick will re-verify.
                _LOGGER.warning(
                    "DodPolicyActuator: post_write read returned None for target=%d",
                    target,
                )
                return
            if post_write != target:
                _LOGGER.error(
                    "DodPolicyActuator: silent fail — target=%d post_write=%s",
                    target,
                    post_write,
                )
                await self._notify_alert(target, post_write, reason="silent_fail")
                return

            self._log_entry(target, previous=current)

    def _read_inverter_dod(self) -> int | None:
        """Read current DoD register value from HA state cache (Goodwe poll)."""
        state = self._hass.states.get(GOODWE_DOD_NUMBER)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return int(float(state.state))
        except (ValueError, TypeError):
            return None

    async def _apply_scene(self, target: int) -> None:
        """Apply target DoD via scene.apply (blocking=True awaits Modbus write)."""
        await self._hass.services.async_call(
            "scene",
            "apply",
            {"entities": {GOODWE_DOD_NUMBER: str(target)}},
            blocking=True,
        )

    def _log_entry(self, target: int, *, previous: int | None) -> None:
        """Write structured logbook entry for traceability of DoD changes."""
        phase = self._ems.dod_policy.current_phase.value
        prev_str = str(previous) if previous is not None else "unknown"
        async_log_entry(
            self._hass,
            name="DodPolicy",
            message=f"target_dod {prev_str} → {target} (phase={phase})",
            domain=DOMAIN,
            entity_id=GOODWE_DOD_NUMBER,
        )
        _LOGGER.info(
            "DodPolicyActuator: applied target_dod=%d (was %s, phase=%s)",
            target,
            prev_str,
            phase,
        )

    async def _notify_alert(
        self, target: int, current: int | None, *, reason: str
    ) -> None:
        """Fire Telegram alert (via existing notify_alert script)."""
        try:
            await self._hass.services.async_call(
                "script",
                "turn_on",
                {
                    "entity_id": NOTIFY_ALERT_SCRIPT,
                    "variables": {
                        "title": "DodPolicy: DoD apply failure",
                        "message": (
                            f"Target {target} not propagated to inverter. "
                            f"Current state: {current}. Reason: {reason}."
                        ),
                    },
                },
                blocking=False,
            )
        except Exception:  # noqa: BLE001 — defensive: don't crash actuator on notify failure
            _LOGGER.exception("DodPolicyActuator: notify_alert call failed")
