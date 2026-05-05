"""Battery state persistence — driven adapter dla HA Storage.

Persists `BatteryManager` state across HA restarts via HA Storage helper.
Domain (`BatteryManager`) jest pure — eksponuje `snapshot()`/`restore(data)`.
Tutaj trzymamy `Store`, dispatchujemy save jako entry-scoped foreground
task (musi przeżyć shutdown — `entry.async_create_task` jest waited
przez `async_block_till_done`, w przeciwieństwie do background_task).

Wzorzec hexagonal: **driven adapter (outbound)** — domain dictates
"save state", konkretna impl wywołuje HA `Store`. Patrz ADR-018.
"""

from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from ..domain.battery import BatteryManager

BATTERY_STORAGE_VERSION: Final[int] = 1
BATTERY_STORAGE_KEY: Final[str] = "smart_rce_battery_manager"


class BatteryStatePersistence:
    """Driven adapter — persists BatteryManager snapshot przez HA Storage."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, manager: BatteryManager
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._manager = manager
        self._store: Store = Store(hass, BATTERY_STORAGE_VERSION, BATTERY_STORAGE_KEY)
        self._last_snapshot: dict | None = None

    async def async_restore(self) -> None:
        """Wywołać RAZ przed pierwszym update() w async_setup_entry."""
        data = await self._store.async_load()
        if data:
            self._manager.restore(data)
        self._last_snapshot = self._manager.snapshot()

    @callback
    def save_if_changed(self) -> None:
        """Persist snapshot na disk gdy zmienił się od ostatniego zapisu.

        Wywoływane jako listener po każdym ems.update_state.
        """
        current = self._manager.snapshot()
        if current == self._last_snapshot:
            return
        self._last_snapshot = current
        self._entry.async_create_task(
            self._hass,
            self._store.async_save(current),
            name="smart_rce_battery_save",
        )
