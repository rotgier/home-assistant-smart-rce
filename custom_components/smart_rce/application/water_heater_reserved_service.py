"""WaterHeaterReservedService — application orchestrator for reserved-power policy.

Public API consumed by HA entities (NumberEntity, SelectEntity) and Ems:
- `compute_current_value(input)` — pure decision (no mutation). Returns
  the effective reserved-power value; Ems passes the result to
  `WaterHeaterManager.update(state, reserved_balanced_full=...)`.
- `manual_value` / `mode` — property reads for UI
- `set_mode(mode)` / `set_manual_value(value)` — UI mutators (async; persist)
- `add_listener(cb)` — single-registry refresh hook (inherited from `Service`)

DDD application layer:
- HASS-unaware (no `hass`, no HA service calls)
- Dependencies injected via constructor (repo + clock)
- No in-memory cache — the computation is pure and inputs come per-tick
  from Ems. Persisted state is mode + manual_value (in repo).
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


class WaterHeaterReservedService(Service[WaterHeaterReservedRepository]):
    """Application service. HASS-unaware — repo + clock injected at construction."""

    def __init__(
        self,
        repo: WaterHeaterReservedRepository,
        clock: Callable[[], datetime],
    ) -> None:
        super().__init__(repo)
        self._clock = clock

    @callback
    def compute_current_value(self, input: WaterHeaterReservedInput) -> int:
        """Return the effective reserved-power value (no mutation).

        Called per-tick from Ems.update_state; the returned value is passed
        as `reserved_balanced_full` kwarg to `WaterHeaterManager.update`.
        Decision logic lives in the policy.
        """
        return self._repo.policy.compute_current_value(self._clock(), input)

    # ─── Properties (entity queries) ───

    @property
    def manual_value(self) -> int:
        return self._repo.policy.manual_value

    @property
    def mode(self) -> ReservedMode:
        return self._repo.policy.mode

    @property
    def prefer_battery_first(self) -> bool:
        """When True, escalate reserved + apply bonus gate (see WaterHeaterManager.target)."""
        return self._repo.policy.prefer_battery_first

    # ─── User mutators ───

    async def set_mode(self, mode: ReservedMode) -> None:
        """UI-driven mode change. Persists + notifies listeners on delta."""
        await self._persist_and_notify(self._repo.policy.set_mode(mode))

    async def set_manual_value(self, value: int) -> None:
        """UI-driven manual_value change. Persists + notifies listeners on delta."""
        await self._persist_and_notify(self._repo.policy.set_manual_value(value))

    async def set_prefer_battery_first(self, value: bool) -> None:
        """UI-driven prefer_battery_first toggle. Persists + notifies listeners on delta."""
        await self._persist_and_notify(
            self._repo.policy.set_prefer_battery_first(value)
        )
