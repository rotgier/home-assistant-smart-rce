"""Repository[T] — base class for aggregate persistence via HA Store.

Template Method pattern: base provides `save_if_changed` + `persist`;
child provides aggregate ref via `_get_aggregate()` + `STORAGE_KEY`
ClassVar. Aggregate T must expose `to_dict() -> dict[str, Any]`. Restore
(`async_restore`) is child's responsibility — aggregate construction varies
(repo-owns: replace; external-policy: mutate fields in place).

Persistence pattern (ADR-018, ~1s crash safety):
- `save_if_changed()` is sync `@callback` — fires `persist` as foreground
  task via `AsyncTaskRunner.run` (must complete on shutdown).
- `persist()` is `await`-able directly from async mutators when immediate
  persistence is required (e.g., BatteryChargeRepository.record_modbus_read
  — actuator drift detection requires disk state before next refresh tick).
- Idempotent: dict-equality guard against `_last_saved`.

Two-phase init:
1. `__init__(hass, tasks)` — base creates Store from STORAGE_KEY/VERSION.
2. `await repo.async_restore()` — child loads + reconstructs aggregate.

Hexagonal pattern: **driven adapter (outbound)** — domain dictates
"save aggregate", concrete impl uses HA `Store`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .async_task_runner import AsyncTaskRunner


class Repository[T](ABC):
    """Base — persists aggregate of type T via HA Store."""

    STORAGE_KEY: ClassVar[str]
    STORAGE_VERSION: ClassVar[int] = 1

    def __init__(self, hass: HomeAssistant, tasks: AsyncTaskRunner) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, self.STORAGE_VERSION, self.STORAGE_KEY
        )
        self._tasks = tasks
        self._last_saved: dict[str, Any] | None = None

    @abstractmethod
    def _get_aggregate(self) -> T:
        """Child returns current aggregate ref (must expose to_dict())."""

    @callback
    def save_if_changed(self) -> None:
        """Sync wrapper — fires persist via AsyncTaskRunner.run."""
        self._tasks.run(self.persist(), name=f"smart_rce_{self.STORAGE_KEY}_save")

    async def persist(self) -> None:
        """Idempotent persist with dict-equality guard.

        Awaitable directly from async mutators (immediate persistence).
        For sync callers use `save_if_changed()` which dispatches via tasks.
        """
        current = self._get_aggregate().to_dict()
        if current == self._last_saved:
            return
        await self._store.async_save(current)
        self._last_saved = current
