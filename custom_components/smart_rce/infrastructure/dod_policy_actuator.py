"""DodPolicyActuator — driven adapter for inverter DoD via number.set_value.

Apply `dod_policy.target_dod` to `number.goodwe_depth_of_discharge_on_grid`
as fire-and-forget background task. Uses `number.set_value` directly (one
entity = no scene.apply orchestration overhead — scene.apply unwraps to
async_reproduce_state which dispatches to the same number platform anyway).
GoodweEmsActuator still uses scene.apply because it sets 2 entities
atomically (select.goodwe_ems_mode + number.goodwe_ems_power_limit).

Three enhancements over the basic write:

1. **Read-back verification**: after each number.set_value, immediately
   re-read inverter state. blocking=True awaits the Goodwe entity's
   async_set_native_value, which write_settings + sets attr + fires
   async_write_ha_state — so HA state cache reflects the new value as
   soon as the await returns. If post-read still diverges → silent
   failure (Modbus reject, integration bug) → Telegram alert.

2. **No persisted `_last_applied`**: the inverter state itself is the source
   of truth. Each tick reads inverter state, compares with target, writes
   only if diverged. Self-healing across restarts and external drift (e.g.
   user manually changes DoD via UI, our next tick converges back to target).

3. **Logbook attribution via Context**: `number.set_value` is called with a
   child Context whose parent fires `smart_rce_action` (phase + reason). HA
   logbook walks the parent_id chain to render the resulting state_changed
   as "triggered by Smart RCE phase=X" — analog to HA's native automation
   describer (avoids the duplicate-entries problem of `async_log_entry`).

Anti-spam + telegram alerts are delegated to `ApplyGuard`.

Hexagonal pattern: **driven adapter (outbound)** — domain (DodPolicy)
dictates target value, concrete impl applies via HA `number.set_value`.
"""

import asyncio
import logging

from homeassistant.core import HomeAssistant, callback

from ..domain.dod_policy import DodPolicy
from .apply_guard import ApplyGuard
from .async_task_runner import AsyncTaskRunner
from .context_chain import fire_action_and_chain_context

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
        self._context_user_id = context_user_id
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
                await self._apply(target, previous=current)
            except Exception:
                _LOGGER.exception(
                    "DodPolicyActuator: number.set_value raised for target=%d",
                    target,
                )
                await self._guard.record_failure(
                    title="Smart RCE: błąd zapisu DoD",
                    message=(
                        f"Nie udało się ustawić DoD na falowniku. "
                        f"Cel {target}, aktualnie {current}. "
                        f"Wyjątek przy zapisie."
                    ),
                )
                return

            if not await self._verify_applied(target):
                return  # mismatch already alerted via guard

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

    async def _apply(self, target: int, *, previous: int) -> None:
        """Apply target DoD via number.set_value (blocking=True awaits Modbus).

        Fires smart_rce_action event with phase/reason metadata first, then
        passes a child Context to number.set_value. HA logbook renders the
        resulting state_changed entry as "DoD changed to N triggered by
        Smart RCE phase=X (reason=...)" via parent_id chain.
        """
        ctx = fire_action_and_chain_context(
            self._hass,
            self._context_user_id,
            phase=self._policy.current_phase.value,
            reason=f"target_dod {previous} → {target}",
        )
        await self._hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": GOODWE_DOD_NUMBER, "value": float(target)},
            blocking=True,
            context=ctx,
        )

    async def _verify_applied(self, target: int) -> bool:
        """Read-back check after number.set_value (silent-fail detection).

        number.set_value blocking=True → after await, inverter state SHOULD
        reflect the new value. If not, silent failure (Modbus reject,
        integration bug).

        Returns:
            True  — target matches post-write state (or transient read=None,
                    next tick re-verifies, no alert)
            False — mismatch detected; failure recorded via guard (alert fired)

        """
        post_write = self._read_inverter_dod()
        if post_write is None:
            # Defensive: state momentarily unavailable post-write. Don't
            # alert — next tick will re-verify.
            _LOGGER.warning(
                "DodPolicyActuator: post_write read returned None for target=%d",
                target,
            )
            return True
        if post_write == target:
            return True
        _LOGGER.error(
            "DodPolicyActuator: silent fail — target=%d post_write=%s",
            target,
            post_write,
        )
        await self._guard.record_failure(
            title="Smart RCE: cichy błąd zapisu DoD",
            message=(
                f"DoD nie propagował się na falownik. "
                f"Cel {target}, po zapisie {post_write}."
            ),
        )
        return False
