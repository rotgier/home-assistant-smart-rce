"""WaterHeaterReservedService — application orchestrator for reserved-power policy.

Public API consumed by HA entities (NumberEntity, SelectEntity) and Ems:
- `update(input)` — per-tick orchestration; caches latest auto value, returns
  current_value (MANUAL→manual_value, AUTO→computed). Ems passes the result
  to `WaterHeaterManager.update(state, reserved_balanced_full=...)`.
- `current_value` / `manual_value` / `mode` — property reads for sensors + UI
- `set_mode(mode)` / `set_manual_value(value)` — UI mutators (async; persist)
- `add_mode_listener(cb)` / `add_manual_value_listener(cb)` — UI refresh hooks

DDD application layer:
- HASS-unaware (no `hass`, no HA service calls)
- Dependencies injected via constructor (repo + clock)
- Auto cache (`_last_auto_value`) is in-memory only — recomputed every tick.
  Persisted state is mode + manual_value (in repo).
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
from datetime import datetime  # noqa: TC003 — used in constructor signature
import logging
from typing import TYPE_CHECKING

from homeassistant.core import callback

from ..domain.water_heater_reserved_policy import ReservedMode, WaterHeaterReservedInput

if TYPE_CHECKING:
    from ..infrastructure.water_heater_reserved_repository import (
        WaterHeaterReservedRepository,
    )

_LOGGER = logging.getLogger(__name__)

# Initial auto-cache value before first update tick — matches policy stub.
_DEFAULT_AUTO_BOOTSTRAP = 3000


class WaterHeaterReservedService:
    """Application service. HASS-unaware — dependencies injected at construction."""

    def __init__(
        self,
        repo: WaterHeaterReservedRepository,
        clock: Callable[[], datetime],
    ) -> None:
        self._repo = repo
        self._clock = clock
        # In-memory cache of last auto-computed value (recomputed every tick
        # by Ems.update_state — persistence is not needed since policy stub
        # is deterministic given inputs).
        self._last_auto_value: int = _DEFAULT_AUTO_BOOTSTRAP
        self._mode_listeners: list[Callable[[ReservedMode], None]] = []
        self._manual_value_listeners: list[Callable[[int], None]] = []

    @callback
    def update(self, input: WaterHeaterReservedInput) -> int:
        """Per-tick orchestration — called from Ems.update_state.

        1. Compute auto value from current inputs (stub returns 3000).
        2. Cache it for property reads between ticks.
        3. Return current_value: manual_value if MANUAL, else auto.
        """
        self._last_auto_value = self._repo.policy.compute_auto(self._clock(), input)
        return self._repo.policy.current_value(self._last_auto_value)

    # ─── Properties (entity / Ems queries) ───

    @property
    def current_value(self) -> int:
        """Last-computed effective value (MANUAL→manual_value, else cached auto)."""
        return self._repo.policy.current_value(self._last_auto_value)

    @property
    def manual_value(self) -> int:
        return self._repo.policy.manual_value

    @property
    def mode(self) -> ReservedMode:
        return self._repo.policy.mode

    @property
    def auto_value(self) -> int:
        """Last-computed auto value (cache; sensor diagnostic)."""
        return self._last_auto_value

    # ─── User mutators ───

    async def set_mode(self, mode: ReservedMode) -> None:
        """UI-driven mode change. Persists + notifies listeners on delta."""
        previous = self._repo.policy.mode
        if previous == mode:
            return
        await self._repo.set_mode(mode)
        _LOGGER.info(
            "WaterHeaterReservedService: mode %s → %s",
            previous.value,
            mode.value,
        )
        self._notify_mode_listeners(mode)

    async def set_manual_value(self, value: int) -> None:
        """UI-driven manual_value change. Persists + notifies listeners on delta."""
        previous = self._repo.policy.manual_value
        if previous == value:
            return
        await self._repo.set_manual_value(value)
        _LOGGER.info(
            "WaterHeaterReservedService: manual_value %s → %s W",
            previous,
            value,
        )
        self._notify_manual_value_listeners(value)

    def add_mode_listener(
        self, cb: Callable[[ReservedMode], None]
    ) -> Callable[[], None]:
        """Subscribe to mode changes. Returns unsubscribe callable."""
        self._mode_listeners.append(cb)

        def _unsub() -> None:
            with contextlib.suppress(ValueError):
                self._mode_listeners.remove(cb)

        return _unsub

    def _notify_mode_listeners(self, mode: ReservedMode) -> None:
        for cb in self._mode_listeners:
            cb(mode)

    def add_manual_value_listener(
        self, cb: Callable[[int], None]
    ) -> Callable[[], None]:
        """Subscribe to manual_value changes. Returns unsubscribe callable."""
        self._manual_value_listeners.append(cb)

        def _unsub() -> None:
            with contextlib.suppress(ValueError):
                self._manual_value_listeners.remove(cb)

        return _unsub

    def _notify_manual_value_listeners(self, value: int) -> None:
        for cb in self._manual_value_listeners:
            cb(value)
