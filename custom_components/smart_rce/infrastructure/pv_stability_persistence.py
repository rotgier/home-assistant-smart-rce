"""PvStability state persistence — driven adapter for HA Storage.

Persists `PvStability` snapshot across HA restarts via HA Storage helper.
Only `run_start` is in the snapshot — transient sensor readings
(`last_derivative_w_per_min`, `last_stability_value`, `last_update`)
refresh from live sensors on the next minute tick after boot, so
persisting them buys nothing and would force per-minute disk writes.

Listener-based pattern (mirrors `DodPolicyPersistence`):
- `async_restore` called once before the first `PvForecastService`
  recalc — hydrates aggregate from disk.
- `save_if_changed` registered as a `PvForecastService` listener; fires
  on every recalc (per-minute via `_recalculate_extrapolated` +
  forecast updates). Compares dict snapshot — writes disk only on real
  `run_start` transitions (~2-4× per day).

Hexagonal pattern: **driven adapter (outbound)** — domain dictates
"save state", concrete impl uses HA `Store`.
"""

from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from ..domain.pv_stability import PvStability

PV_STABILITY_STORAGE_VERSION: Final[int] = 1
PV_STABILITY_STORAGE_KEY: Final[str] = "smart_rce_pv_stability"


class PvStabilityPersistence:
    """Driven adapter — persists PvStability snapshot via HA Storage."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, stability: PvStability
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._stability = stability
        self._store: Store = Store(
            hass, PV_STABILITY_STORAGE_VERSION, PV_STABILITY_STORAGE_KEY
        )
        self._last_snapshot: dict | None = None

    async def async_restore(self) -> None:
        """Call ONCE before first update — hydrate `run_start` from disk."""
        data = await self._store.async_load()
        if data:
            restored = PvStability.from_dict(data)
            self._stability.run_start = restored.run_start
        self._last_snapshot = self._stability.to_dict()

    @callback
    def save_if_changed(self) -> None:
        """Persist snapshot to disk when changed since last save.

        Registered as `PvForecastService` listener — fires after every
        `_notify_listeners` (per-minute tick + forecast updates). Compares
        `to_dict()` snapshot (only `run_start`) → no-op on minutes where
        the stable run hasn't flipped.
        """
        current = self._stability.to_dict()
        if current == self._last_snapshot:
            return
        self._last_snapshot = current
        self._entry.async_create_task(
            self._hass,
            self._store.async_save(current),
            name="smart_rce_pv_stability_save",
        )
