"""GoodweEmsActuator — driven adapter dla Goodwe EMS via scene.apply.

Apply target `EmsOperation` (ems_mode + power_limit_w) to Goodwe inverter
entities (`select.goodwe_ems_mode` + `number.goodwe_ems_power_limit`)
as fire-and-forget background tasks. `scene.apply` writes both entities
atomically in one HA service call (1 → async_reproduce_state → per-domain
dispatch).

Unification of the previous `GridExportActuator` — taking an explicit
`target: EmsOperation` instead of reading manager.recommended_*. The
caller (Ems) computes the final EmsOperation (Etap F will add schedule
precedence over grid_export) and passes it here for application.

State-diff coalescing — `_dispatch` re-reads observed inverter state
under the lock (`_read_inverter_state`) and writes only when the target
differs. No internal `_last_applied` cache: the inverter itself is the
source of truth, so smart_rce restarts / external automation writes
do not desync us.

Wzorzec:
1. `@callback apply_if_changed(target)` (sync) — spawn fire-and-forget
   background task. Brak dedup tutaj — task spawn jest tani.
2. `_dispatch(target)` (async) — `async with lock` → read inverter →
   `target.matches_inverter(...)` → `scene.apply` only on delta →
   read-back verification (silent-fail detection).

Lock daje:
- **Modbus serialization** — żaden wire interleave między concurrent
  scene.apply calls.
- **Coalescing** — burst N event'ów spawnuje N tasków; lock + re-read
  zostawia 1 actual scene.apply.

Read-back verification (parity z DodPolicyActuator): after scene.apply
blocking=True returns, re-read inverter state and compare with target.
On mismatch → ApplyGuard.record_failure (telegram alert + counter).

Anti-spam + telegram alerts delegated to ApplyGuard.

Wzorzec hexagonal: **driven adapter (outbound)** — domain dictates the
target EmsOperation, this adapter writes the inverter. Patrz ADR-019.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from homeassistant.core import HomeAssistant, callback

from ..domain.ems_operation import EmsOperation
from .apply_guard import ApplyGuard
from .async_task_runner import AsyncTaskRunner
from .context_chain import fire_action_and_chain_context

_LOGGER = logging.getLogger(__name__)

GOODWE_EMS_MODE_SELECT = "select.goodwe_ems_mode"
GOODWE_EMS_POWER_LIMIT_NUMBER = "number.goodwe_ems_power_limit"


class GoodweEmsActuator:
    """Driven adapter — apply EmsOperation to Goodwe via scene.apply."""

    def __init__(
        self,
        hass: HomeAssistant,
        tasks: AsyncTaskRunner,
        *,
        context_user_id: str,
    ) -> None:
        self._hass = hass
        self._tasks = tasks
        self._context_user_id = context_user_id
        self._guard = ApplyGuard(hass, "GoodweEmsActuator")
        self._lock = asyncio.Lock()

    @callback
    def apply_if_changed(self, target: EmsOperation) -> None:
        """Spawn fire-and-forget background task with the given target."""
        self._tasks.run_background(
            self._dispatch(target), name="smart_rce_goodwe_ems_apply"
        )

    async def _dispatch(self, target: EmsOperation) -> None:
        async with self._lock:
            current_mode, current_xset = self._read_inverter_state()
            if current_mode is None:
                # Goodwe integration not yet loaded (typical at HA startup —
                # smart_rce loads before goodwe) or entity transiently
                # unavailable. Skip without alert; next ems tick will retry
                # once the goodwe entity is ready. Without this guard the
                # apply would proceed (matches_inverter(None,_) → False),
                # scene.apply would raise on the missing entity, and
                # ApplyGuard would spam telegram on every HA startup.
                _LOGGER.debug(
                    "GoodweEmsActuator: skipping — inverter entity unavailable"
                )
                return
            if target.matches_inverter(current_mode, current_xset):
                return
            if self._guard.should_skip():
                return

            try:
                await self._apply_scene(target)
            except Exception:
                _LOGGER.exception(
                    "GoodweEmsActuator: scene.apply raised for mode=%s xset=%s",
                    target.ems_mode,
                    target.power_limit_w,
                )
                await self._guard.record_failure(
                    title="Smart RCE: błąd zapisu EMS",
                    message=(
                        f"Nie udało się ustawić trybu EMS na falowniku. "
                        f"Cel: mode={target.ems_mode}, xset={target.power_limit_w}. "
                        f"Aktualnie: mode={current_mode}, xset={current_xset}. "
                        f"Źródło: {target.source}."
                    ),
                )
                return

            if not await self._verify_applied(target):
                return  # mismatch already alerted via guard

            self._guard.record_success()
            _LOGGER.info(
                "GoodweEmsActuator applied mode=%s xset=%s (source=%s reason=%s)",
                target.ems_mode,
                target.power_limit_w,
                target.source,
                target.reason,
            )

    def _read_inverter_state(self) -> tuple[str | None, int | None]:
        """Read observed Goodwe (mode, xset) from hass.states.

        Returns (None, None) when the Goodwe entity is unavailable.
        `_dispatch` treats None mode as "goodwe not loaded yet" and skips
        the apply (avoids scene.apply on missing entity → telegram spam
        at startup).
        """
        mode_state = self._hass.states.get(GOODWE_EMS_MODE_SELECT)
        if mode_state is None or mode_state.state in ("unknown", "unavailable"):
            return (None, None)
        mode = mode_state.state

        xset_state = self._hass.states.get(GOODWE_EMS_POWER_LIMIT_NUMBER)
        xset: int | None = None
        if xset_state is not None and xset_state.state not in (
            "unknown",
            "unavailable",
        ):
            with contextlib.suppress(ValueError, TypeError):
                xset = int(float(xset_state.state))
        return (mode, xset)

    async def _apply_scene(self, target: EmsOperation) -> None:
        """Apply target via scene.apply — 2 entities written atomically.

        Fires smart_rce_action event with phase/reason metadata first, then
        passes a child Context to scene.apply. HA logbook renders the
        resulting state_changed entries as "EMS mode changed to X triggered
        by Smart RCE phase=Y (reason=...)" via parent_id chain.
        """
        # scene.apply expects string-valued state per HA core's _convert_states.
        # power_limit_w only passed for active modes — Goodwe ignores it on auto.
        entities: dict[str, str] = {GOODWE_EMS_MODE_SELECT: target.ems_mode}
        if (
            target.power_limit_w is not None
            and target.power_limit_w >= 0
            and target.ems_mode != "auto"
        ):
            entities[GOODWE_EMS_POWER_LIMIT_NUMBER] = str(target.power_limit_w)
        ctx = fire_action_and_chain_context(
            self._hass,
            self._context_user_id,
            phase=target.source,
            reason=target.reason,
        )
        await self._hass.services.async_call(
            "scene",
            "apply",
            {"entities": entities},
            blocking=True,
            context=ctx,
        )

    async def _verify_applied(self, target: EmsOperation) -> bool:
        """Read-back check after scene.apply (silent-fail detection).

        scene.apply blocking=True → after await, inverter state SHOULD reflect
        the new value. If not, silent failure (Modbus reject, integration bug).

        Returns:
            True  — target matches post-write state (or transient read=None,
                    next tick re-verifies, no alert)
            False — mismatch detected; failure recorded via guard (alert fired)

        """
        post_mode, post_xset = self._read_inverter_state()
        if post_mode is None:
            # Defensive: state momentarily unavailable post-write. Don't
            # alert — next tick will re-verify.
            _LOGGER.warning(
                "GoodweEmsActuator: post_write read returned None for mode=%s xset=%s",
                target.ems_mode,
                target.power_limit_w,
            )
            return True
        if target.matches_inverter(post_mode, post_xset):
            return True
        _LOGGER.error(
            "GoodweEmsActuator: silent fail — target mode=%s xset=%s post_write mode=%s xset=%s",
            target.ems_mode,
            target.power_limit_w,
            post_mode,
            post_xset,
        )
        await self._guard.record_failure(
            title="Smart RCE: cichy błąd zapisu EMS",
            message=(
                f"Tryb EMS nie propagował się na falownik. "
                f"Cel: mode={target.ems_mode}, xset={target.power_limit_w}. "
                f"Po zapisie: mode={post_mode}, xset={post_xset}. "
                f"Źródło: {target.source}."
            ),
        )
        return False
