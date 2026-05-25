"""GoodweEmsActuator — driven adapter dla Goodwe EMS via scene.apply.

Apply target `EmsOperation` (ems_mode + power_limit_w) to Goodwe inverter
entities (`select.goodwe_ems_mode` + `number.goodwe_ems_power_limit`)
as fire-and-forget background tasks.

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
   `target.matches_inverter(...)` → `scene.apply` only on delta.

Lock daje:
- **Modbus serialization** — żaden wire interleave między concurrent
  scene.apply calls.
- **Coalescing** — burst N event'ów spawnuje N tasków; lock + re-read
  zostawia 1 actual scene.apply.

Wzorzec hexagonal: **driven adapter (outbound)** — domain dictates the
target EmsOperation, this adapter writes the inverter. Patrz ADR-019.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from homeassistant.core import Context, HomeAssistant, callback

from ..domain.ems_operation import EmsOperation
from .apply_guard import ApplyGuard
from .async_task_runner import AsyncTaskRunner

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
        self._context = Context(user_id=context_user_id)
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
            if target.matches_inverter(current_mode, current_xset):
                return
            if self._guard.should_skip():
                return
            await self._apply_scene(target, current_mode, current_xset)

    def _read_inverter_state(self) -> tuple[str | None, int | None]:
        """Read observed Goodwe (mode, xset) from hass.states.

        Returns (None, None) when the Goodwe entity is unavailable —
        treated as "unknown current state", which forces an apply on the
        first dispatch (defensive: better to push state once than skip
        forever if entity boot-up race kept the entity unknown).
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

    async def _apply_scene(
        self,
        target: EmsOperation,
        current_mode: str | None,
        current_xset: int | None,
    ) -> None:
        # scene.apply expects string-valued state per HA core's _convert_states.
        # power_limit_w only passed for active modes — Goodwe ignores it on auto.
        entities: dict[str, str] = {GOODWE_EMS_MODE_SELECT: target.ems_mode}
        if (
            target.power_limit_w is not None
            and target.power_limit_w >= 0
            and target.ems_mode != "auto"
        ):
            entities[GOODWE_EMS_POWER_LIMIT_NUMBER] = str(target.power_limit_w)
        try:
            await self._hass.services.async_call(
                "scene",
                "apply",
                {"entities": entities},
                blocking=True,
                context=self._context,
            )
        except Exception:
            _LOGGER.exception(
                "Failed to apply EmsOperation mode=%s xset=%s",
                target.ems_mode,
                target.power_limit_w,
            )
            await self._guard.record_failure(
                title="GoodweEms: apply failure",
                message=(
                    f"Target mode={target.ems_mode} xset={target.power_limit_w} "
                    f"failed. Current state: mode={current_mode} xset={current_xset}. "
                    f"Source: {target.source}."
                ),
            )
            return
        self._guard.record_success()
        _LOGGER.info(
            "GoodweEmsActuator applied mode=%s xset=%s (source=%s reason=%s)",
            target.ems_mode,
            target.power_limit_w,
            target.source,
            target.reason,
        )
