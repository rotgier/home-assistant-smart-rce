"""DodPolicyRepository — driven adapter for DodPolicy persistence.

Persists `DodPolicy` state across HA restarts via HA Storage helper (ADR-018).
Persisted state:
- target_dod (informational — current value also readable from inverter)
- current_phase (diagnostic + UNKNOWN keep-state source)
- _override_set_phase (override expiry tracking — survives restart so
  user-set override remains active until phase boundary)
- _prev_block (hysteresis keep-state for delegating phases)

Persistence pattern (consistent with `BatteryScheduleRepository`):
- `save_if_changed()` is sync `@callback` — fires foreground task
  (`AsyncTaskRunner.run`, must complete before HA shutdown).
- Idempotent: `_persist()` re-checks dict equality.

Renamed from `DodPolicyPersistence` (Etap 0 follow-up) for DDD consistency
with `BatteryScheduleRepository` naming.

Hexagonal pattern: **driven adapter (outbound)** — domain dictates
"save state", concrete impl uses HA `Store`.
"""

from typing import Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from ..domain.dod_policy import DodPolicy
from .async_task_runner import AsyncTaskRunner

DOD_POLICY_STORAGE_VERSION: Final[int] = 1
DOD_POLICY_STORAGE_KEY: Final[str] = "ems_dod_policy"


class DodPolicyRepository:
    """Driven adapter — persists DodPolicy snapshot via HA Storage."""

    def __init__(
        self,
        hass: HomeAssistant,
        policy: DodPolicy,
        tasks: AsyncTaskRunner,
    ) -> None:
        self._policy = policy
        self._tasks = tasks
        self._store: Store[dict[str, Any]] = Store(
            hass, DOD_POLICY_STORAGE_VERSION, DOD_POLICY_STORAGE_KEY
        )
        self._last_snapshot: dict[str, Any] | None = None

    async def async_restore(self) -> None:
        """Call ONCE before first update_state in async_setup_entry."""
        data = await self._store.async_load()
        if data:
            restored = DodPolicy.from_dict(data)
            self._policy.target_dod = restored.target_dod
            self._policy.current_phase = restored.current_phase
            self._policy._override_set_phase = restored._override_set_phase  # noqa: SLF001 — restoring private state
            self._policy._prev_block = restored._prev_block  # noqa: SLF001 — restoring private state
        self._last_snapshot = self._policy.to_dict()

    @callback
    def save_if_changed(self) -> None:
        """Sync — fires foreground task to persist if policy state changed."""
        self._tasks.run(self._persist(), name="smart_rce_dod_policy_save")

    async def _persist(self) -> None:
        """Private — actual async save (called via tasks.run from save_if_changed)."""
        current = self._policy.to_dict()
        if current == self._last_snapshot:
            return
        self._last_snapshot = current
        await self._store.async_save(current)
