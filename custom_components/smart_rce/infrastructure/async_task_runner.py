"""AsyncTaskRunner — fire-and-forget tasks tied to a config entry lifecycle.

Wraps `entry.async_create_task(hass, coro, name=...)` so callers don't need
to keep both hass and entry references just to schedule async work. Single
instance per config entry, instantiated in `ems_factory.create_ems` and
injected via constructor into repositories / application services that
need to fire async work (persistence saves, notifications, etc.).

Tied to entry → all spawned tasks are cancelled when the entry is unloaded
(no orphan tasks after config_entry reload).
"""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


class AsyncTaskRunner:
    """Schedule fire-and-forget coroutines tied to a config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry

    def run(self, coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> object:
        """Schedule `coro` as a fire-and-forget task tied to this entry.

        Returns the task object (rarely useful — caller treats as void).
        """
        return self._entry.async_create_task(self._hass, coro, name=name)
