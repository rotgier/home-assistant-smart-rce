"""DodPolicyActuator — driven adapter for inverter DoD via scene.apply.

Apply `dod_policy.target_dod` to `number.goodwe_depth_of_discharge_on_grid`
as fire-and-forget background task. Mirrors `goodwe_ems_actuator.py` pattern
(ADR-019) with three enhancements:

1. **Read-back verification**: after each scene.apply, immediately re-read
   inverter state. scene.apply blocking=True awaits Modbus write + state
   refresh, so post-write state SHOULD reflect new value. If not → silent
   failure → Telegram alert.

2. **No persisted `_last_applied`**: the inverter state itself is the source
   of truth. Each tick reads inverter state, compares with target, writes
   only if diverged. Self-healing across restarts and external drift (e.g.
   user manually changes DoD via UI, our next tick converges back to target).

3. **Logbook attribution via Context**: scene.apply is called with a
   `Context(user_id=...)` tied to the "Smart RCE" system user. HA logbook
   resolves it to "Changed by Smart RCE" automatically — no custom logbook
   entries needed (avoids the duplicate-entries problem of `async_log_entry`).

Anti-spam + telegram alerts are delegated to `ApplyGuard`.

Hexagonal pattern: **driven adapter (outbound)** — domain (DodPolicy)
dictates target value, concrete impl applies via HA `scene.apply` service.
"""

import asyncio
import logging

from homeassistant.core import Context, HomeAssistant, callback

from ..domain.dod_policy import DodPolicy
from .apply_guard import ApplyGuard
from .async_task_runner import AsyncTaskRunner

_LOGGER = logging.getLogger(__name__)

GOODWE_DOD_NUMBER = "number.goodwe_depth_of_discharge_on_grid"


class DodPolicyActuator:
    """Driven adapter — applies DodPolicy.target_dod to inverter."""

    def __init__(
        self,
        hass: HomeAssistant,
        policy: DodPolicy,
        tasks: AsyncTaskRunner,
        *,
        context_user_id: str,
    ) -> None:
        self._hass = hass
        self._policy = policy
        self._tasks = tasks
        self._context = Context(user_id=context_user_id)
        self._guard = ApplyGuard(hass, "DodPolicyActuator")
        self._lock = asyncio.Lock()

    @callback
    def apply_if_changed(self) -> None:
        """Spawn fire-and-forget background task (registered as ems listener)."""
        self._tasks.run_background(self._dispatch(), name="smart_rce_dod_apply")

    async def _dispatch(self) -> None:
        async with self._lock:
            target = self._policy.target_dod
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

            if self._guard.should_skip():
                return

            try:
                await self._apply_scene(target)
            except Exception:
                _LOGGER.exception(
                    "DodPolicyActuator: scene.apply raised for target=%d", target
                )
                await self._guard.record_failure(
                    title="DodPolicy: DoD apply failure",
                    message=(
                        f"Target {target} not propagated to inverter. "
                        f"Current state: {current}. Reason: apply_exception."
                    ),
                )
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
                await self._guard.record_failure(
                    title="DodPolicy: DoD apply failure",
                    message=(
                        f"Target {target} not propagated to inverter. "
                        f"Current state: {post_write}. Reason: silent_fail."
                    ),
                )
                return

            self._guard.record_success()
            _LOGGER.info(
                "DodPolicyActuator: applied target_dod=%d (was %d, phase=%s)",
                target,
                current,
                self._policy.current_phase.value,
            )

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
        """Apply target DoD via scene.apply (blocking=True awaits Modbus write).

        `context=self._context` attributes the resulting state_changed event
        to the "Smart RCE" system user — HA logbook renders this as
        "Changed by Smart RCE" automatically.
        """
        await self._hass.services.async_call(
            "scene",
            "apply",
            {"entities": {GOODWE_DOD_NUMBER: str(target)}},
            blocking=True,
            context=self._context,
        )
