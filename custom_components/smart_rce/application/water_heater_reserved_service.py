"""WaterHeaterReservedService — application orchestrator for reserved-power policy.

Public API consumed by HA entities (NumberEntity, SelectEntity) and Ems:
- `update(input)` — per-tick orchestration; caches latest auto value, returns
  current_value (MANUAL→manual_value, AUTO→computed). Ems passes the result
  to `WaterHeaterManager.update(state, reserved_balanced_full=...)`.
- `current_value` / `manual_value` / `mode` — property reads for sensors + UI
- `set_mode(mode)` / `set_manual_value(value)` — UI mutators (async; persist)
- `add_listener(cb)` — single-registry refresh hook (inherited from `Service`)

DDD application layer:
- HASS-unaware (no `hass`, no HA service calls)
- Dependencies injected via constructor (repo + clock)
- Auto cache (`_last_auto_value`) is in-memory only — recomputed every tick.
  Persisted state is mode + manual_value (in repo).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime  # noqa: TC003 — used in constructor signature

from homeassistant.core import callback

from ..domain.water_heater_reserved_policy import ReservedMode, WaterHeaterReservedInput
from ..infrastructure.water_heater_reserved_repository import (
    WaterHeaterReservedRepository,
)
from .service import Service

# Initial auto-cache value before first update tick — matches policy stub.
_DEFAULT_AUTO_BOOTSTRAP = 3000


class WaterHeaterReservedService(Service[WaterHeaterReservedRepository]):
    """Application service. HASS-unaware — repo + clock injected at construction."""

    def __init__(
        self,
        repo: WaterHeaterReservedRepository,
        clock: Callable[[], datetime],
    ) -> None:
        super().__init__(repo)
        self._clock = clock
        # In-memory cache of last auto-computed value (recomputed every tick
        # by Ems.update_state — persistence is not needed since policy stub
        # is deterministic given inputs).
        self._last_auto_value: int = _DEFAULT_AUTO_BOOTSTRAP

    @callback
    def update(self, input: WaterHeaterReservedInput) -> int:
        """Per-tick orchestration — called from Ems.update_state.

        1. Compute auto value from current inputs (stub returns 3000).
        2. Cache it for property reads between ticks.
        3. Return current_value: manual_value if MANUAL, else auto.
        """
        self._last_auto_value = self._repo.policy.compute_auto(self._clock(), input)
        return self._repo.policy.current_value(self._last_auto_value)

    # ─── Properties (entity queries) ───

    @property
    def manual_value(self) -> int:
        return self._repo.policy.manual_value

    @property
    def mode(self) -> ReservedMode:
        return self._repo.policy.mode

    # ─── User mutators ───

    async def set_mode(self, mode: ReservedMode) -> None:
        """UI-driven mode change. Persists + notifies listeners on delta."""
        await self._persist_and_notify(self._repo.policy.set_mode(mode))

    async def set_manual_value(self, value: int) -> None:
        """UI-driven manual_value change. Persists + notifies listeners on delta."""
        await self._persist_and_notify(self._repo.policy.set_manual_value(value))
