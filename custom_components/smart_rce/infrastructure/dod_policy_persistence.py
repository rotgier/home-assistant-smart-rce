"""DodPolicy state persistence — driven adapter for HA Storage.

Persists `DodPolicy` state across HA restarts via HA Storage helper
(ADR-018). Persisted state:
- target_dod (informational — current value also readable from inverter)
- current_phase (diagnostic + UNKNOWN keep-state source)
- _override_set_phase (override expiry tracking — survives restart so
  user-set override remains active until phase boundary)
- _prev_block (hysteresis keep-state for delegating phases)

Hexagonal pattern: **driven adapter (outbound)** — domain dictates
"save state", concrete impl uses HA `Store`.
"""

from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from ..domain.dod_policy import DodPolicy

DOD_POLICY_STORAGE_VERSION: Final[int] = 1
DOD_POLICY_STORAGE_KEY: Final[str] = "smart_rce_dod_policy"


class DodPolicyPersistence:
    """Driven adapter — persists DodPolicy snapshot via HA Storage."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, policy: DodPolicy
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._policy = policy
        self._store: Store = Store(
            hass, DOD_POLICY_STORAGE_VERSION, DOD_POLICY_STORAGE_KEY
        )
        self._last_snapshot: dict | None = None

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
        """Persist snapshot to disk when changed since last save.

        Registered as ems listener — fires after every Ems.update_state.
        """
        current = self._policy.to_dict()
        if current == self._last_snapshot:
            return
        self._last_snapshot = current
        self._entry.async_create_task(
            self._hass,
            self._store.async_save(current),
            name="smart_rce_dod_policy_save",
        )
