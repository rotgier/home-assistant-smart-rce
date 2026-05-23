"""DodPolicyRepository — owns + persists DodPolicy across HA restarts.

Extends `Repository[DodPolicy]`. Persisted state (ADR-018):
- target_dod (informational — current value also readable from inverter)
- current_phase (diagnostic + UNKNOWN keep-state source)
- _override_set_phase (override expiry tracking — survives restart so
  user-set override remains active until phase boundary)
- _prev_block (hysteresis keep-state for delegating phases)

Two-phase init:
1. `__init__(hass, tasks)` — constructs default policy + Store
2. `await repo.async_restore()` — loads persisted state if present

Hexagonal pattern: **driven adapter (outbound)** — domain dictates
"save state", concrete impl uses HA `Store`.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from ..domain.dod_policy import DodPolicy
from .async_task_runner import AsyncTaskRunner
from .repository import Repository


class DodPolicyRepository(Repository[DodPolicy]):
    """Persists DodPolicy via HA Store. Owns the policy aggregate."""

    STORAGE_KEY = "ems_dod_policy"

    def __init__(self, hass: HomeAssistant, tasks: AsyncTaskRunner) -> None:
        super().__init__(hass, tasks)
        self._policy: DodPolicy = DodPolicy()

    @property
    def policy(self) -> DodPolicy:
        return self._policy

    def _get_aggregate(self) -> DodPolicy:
        return self._policy

    async def async_restore(self) -> None:
        """Call ONCE before Ems first tick. Replaces default policy with persisted."""
        data: dict[str, Any] | None = await self._store.async_load()
        if data is not None:
            self._policy = DodPolicy.from_dict(data)
            self._last_saved = data
