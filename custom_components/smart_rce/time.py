"""Smart RCE time platform — POC TimeEntity with own immediate-persist Store.

POC purpose: verify that a smart_rce-owned TimeEntity is editable from the UI
exactly like an `input_datetime` (has_time=true) helper, while persisting its
value to its own `.storage/smart_rce_test_time` file with effectively-zero
delay (SAVE_DELAY=0 → scheduled on next event loop iteration).

Contrast with HA's input_* helpers: their value persistence goes through
`RestoreStateData` which dumps the entire state machine every 15 minutes
(`homeassistant/helpers/restore_state.py:30`). For battery operation intent
(discharge windows, target SoC) we need immediate persistence so a crash
seconds after the user changes a value doesn't lose it.
"""

from __future__ import annotations

from datetime import time
import logging
from typing import Any

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store

PARALLEL_UPDATES = 1
STORAGE_VERSION = 1
STORAGE_KEY = "smart_rce_test_time"
SAVE_DELAY = 0  # immediate persist on next loop iteration

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the POC TimeEntity from a config entry."""
    store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    data = await store.async_load() or {}
    initial: time | None = None
    if iso := data.get("value"):
        try:
            initial = time.fromisoformat(iso)
        except ValueError:
            _LOGGER.warning("Invalid stored time %r, ignoring", iso)
    async_add_entities([SmartRceTestTime(entry, store, initial)])


class SmartRceTestTime(TimeEntity):
    """POC time entity that persists its value immediately to a private Store."""

    _attr_has_entity_name = False
    _attr_name = "Smart RCE Test Time"
    _attr_should_poll = False

    def __init__(
        self,
        entry: ConfigEntry,
        store: Store[dict[str, Any]],
        initial: time | None,
    ) -> None:
        self._store = store
        self._attr_unique_id = f"{entry.entry_id}_test_time"
        self._attr_native_value = initial

    async def async_set_value(self, value: time) -> None:
        """Persist + update state. Called by service `time.set_value` and UI picker."""
        self._attr_native_value = value
        self._store.async_delay_save(lambda: {"value": value.isoformat()}, SAVE_DELAY)
        self.async_write_ha_state()
