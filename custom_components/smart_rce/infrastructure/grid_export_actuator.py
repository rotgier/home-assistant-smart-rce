"""GridExportActuator — driven adapter dla Goodwe EMS via scene.apply.

Apply Goodwe EMS recommendations (`select.goodwe_ems_mode` +
`number.goodwe_ems_power_limit`) as fire-and-forget background tasks.

Czyta `ems.grid_export.recommended_*` IN-MEMORY (bez round-trip przez
output sensors). Rejestrowany przez `ems.async_add_listener(apply_if_changed)`
— odpala się po każdym `ems.update_state` (state_changed / update_hourly).

Wzorzec:
1. `@callback apply_if_changed` (sync) — spawn fire-and-forget background
   task. Brak dedup tutaj — task spawn jest tani (eager_start=True +
   uncontested asyncio.Lock.acquire = no yield, fast-path skip task
   registration w config_entries.py:1383-1388).
2. `_dispatch` (async) — `async with lock` → re-read in-memory →
   dedup vs `_last_applied` → `scene.apply`.

Lock daje:
- **Modbus serialization** — żaden wire interleave między concurrent
  scene.apply calls (Goodwe lib może mieć per-connection lock, ale
  ordering z naszej perspektywy niezdefiniowany bez tego).
- **Coalescing** — burst N event'ów spawnuje N tasków; lock + re-read
  zostawia 1 actual scene.apply (vs N bez locka).

`entry.async_create_background_task` — task auto-cancels przy entry
unload + shutdown stage 2. Modbus mid-write przerwany jest OK
(hardware utrzyma prev state).

Wzorzec hexagonal: **driven adapter (outbound)** — domain dictates
recommended state, konkretna impl wywołuje HA `scene.apply`. Patrz ADR-019.
"""

import asyncio
import contextlib
import logging

from homeassistant.core import HomeAssistant, callback

from ..domain.grid_export import GridExportManager
from ..domain.input_state import InputState
from .async_task_runner import AsyncTaskRunner

_LOGGER = logging.getLogger(__name__)

GOODWE_EMS_MODE_SELECT = "select.goodwe_ems_mode"
GOODWE_EMS_POWER_LIMIT_NUMBER = "number.goodwe_ems_power_limit"


class GridExportActuator:
    """Driven adapter — Apply Goodwe EMS mode/xset via scene.apply."""

    def __init__(
        self,
        hass: HomeAssistant,
        manager: GridExportManager,
        tasks: AsyncTaskRunner,
    ) -> None:
        self._hass = hass
        self._manager = manager
        self._tasks = tasks
        self._lock = asyncio.Lock()
        # (mode, xset) — ostatnio zaaplikowana para. Init z aktualnego stanu
        # Goodwe entity (kalibracja po reload smart_rce — Goodwe entities
        # persystują, ich state w hass.states jest świeży). Pierwszy dispatch
        # dedupuje gdy recommendation == current Goodwe state, brak spurious
        # apply. None gdy Goodwe entity unavailable (fallback: defensive
        # apply na pierwszy dispatch).
        self._last_applied: tuple[str, int | None] | None = (
            self._read_current_goodwe_state()
        )

    def _read_current_goodwe_state(self) -> tuple[str, int | None] | None:
        """Read current Goodwe (mode, xset) z hass.states dla _last_applied init.

        W trybie 'auto' Goodwe ignoruje xset (rejestr 47512 nieużywany) —
        normalizujemy xset do None, żeby pasowało do domain semantyki
        (`recommended_xset=None` gdy `recommended_ems_mode='auto'`).
        Bez tego: po poprzedniej intervention xset hardware zostaje na
        ostatniej wartości (np. 6000), a recommendation auto+None →
        tuple differ → spurious apply.
        """
        mode_state = self._hass.states.get(GOODWE_EMS_MODE_SELECT)
        if mode_state is None or mode_state.state in ("unknown", "unavailable"):
            return None
        mode = mode_state.state

        if mode == "auto":
            # Domain semantyka: auto mode = xset N/A. Ignorujemy raw hardware.
            return (mode, None)

        xset: int | None = None
        xset_state = self._hass.states.get(GOODWE_EMS_POWER_LIMIT_NUMBER)
        if xset_state is not None and xset_state.state not in (
            "unknown",
            "unavailable",
        ):
            with contextlib.suppress(ValueError, TypeError):
                xset = int(float(xset_state.state))

        return (mode, xset)

    @callback
    def apply_if_changed(self, state: InputState) -> None:
        """Spawn fire-and-forget background task (called from Ems body).

        `state` passed by caller — eliminates back-reference to Ems and keeps
        the actuator's contract narrow (manager + current tick's InputState).

        Skips dispatch when external EMS automation is managing Goodwe this
        hour (`other_ems_automation_active_this_hour=True`). Resets
        `_last_applied` to (auto, None) on the first such tick — assumes the
        external automation will return Goodwe to AUTO at the end of its run.
        Without this reset, after the automation finishes smart_rce would
        compare against a stale `_last_applied` (e.g. last intervention
        values from before the automation) and re-apply that obsolete state.
        """
        if state.other_ems_automation_active_this_hour is True:
            # External automation manages Goodwe — assume it will reset to AUTO.
            # Skip dispatch + sync `_last_applied` so smart_rce does not push
            # stale recommendation when intervention reactivates.
            self._last_applied = ("auto", None)
            return
        self._tasks.run_background(self._dispatch(), name="smart_rce_grid_export_apply")

    async def _dispatch(self) -> None:
        async with self._lock:
            # Re-read in-memory INSIDE locka — między schedule a acquire
            # mogły dojść kolejne event'y; używamy najświeższych wartości.
            mode = self._manager.recommended_ems_mode
            xset = self._manager.recommended_xset
            target = (mode, xset)
            if target == self._last_applied:
                return  # coalesce: same as last apply
            if mode is None:
                return  # invalid, skip without caching
            self._last_applied = target
            await self._apply_scene(mode, xset)

    async def _apply_scene(self, mode: str, xset: int | None) -> None:
        # scene.apply wymaga state jako string (homeassistant/scene.py:58
        # `_convert_states` raises na non-string). number/reproduce_state.py:24
        # parsuje przez float(state.state).
        entities: dict[str, str] = {GOODWE_EMS_MODE_SELECT: mode}
        if xset is not None and xset >= 0:
            entities[GOODWE_EMS_POWER_LIMIT_NUMBER] = str(xset)
        try:
            await self._hass.services.async_call(
                "scene",
                "apply",
                {"entities": entities},
                blocking=True,
            )
            _LOGGER.info(
                "GridExportActuator applied mode=%s xset=%s",
                mode,
                xset,
            )
        except Exception:
            _LOGGER.exception(
                "Failed to apply grid export recommendation mode=%s xset=%s",
                mode,
                xset,
            )
