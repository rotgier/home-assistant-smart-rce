"""Base for application services — listener registry + persist helper.

Services mediate between repository-owned aggregates and HA entities.
HA entities subscribe via `add_listener(cb=self.async_write_ha_state)` to
refresh themselves on any state change inside the bounded context.

Listeners are parameterless — HA core dedupes state-equal writes so per-field
fan-out is unnecessary. A single per-service registry covers both UI-driven
mutations (set_X) and side-channel mutations (event-driven sync, e.g.
`BatteryChargeService.handle_start_charge_today_changed`).

`_persist_and_notify(changed)` is the one-liner mutator helper — pass the
result of `policy.set_X(value)`. When the mutator returned True, persist
the aggregate via the repo and wake all subscribers.

`TRepo` generic param exposes typed `self._repo` to subclasses; bound to
the `Repository[T]` protocol (must have async `persist()`).
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
from typing import Protocol

from homeassistant.core import callback


class _RepoProto(Protocol):
    """Repository contract used by `Service` — persist + save_if_changed."""

    async def persist(self) -> None: ...

    def save_if_changed(self) -> None: ...


class Service[TRepo: _RepoProto]:
    """Base — listener registry + notify dispatch + persist-and-notify helpers."""

    def __init__(self, repo: TRepo) -> None:
        self._repo: TRepo = repo
        self._listeners: list[Callable[[], None]] = []

    async def _persist_and_notify(self, changed: bool) -> None:
        """Async: persist + notify on True (mutator-returned-bool style).

        Use from async UI mutators where the caller awaits durability:
            await self._persist_and_notify(self._repo.policy.set_X(value))

        Idempotent: when nothing changed (`changed == False`), no-op.
        """
        if changed:
            await self._repo.persist()
            self._notify_all()

    @callback
    def _save_if_changed_and_notify(self, changed: bool) -> None:
        """Sync: fire-and-forget save + notify on True.

        Use from sync event-driven handlers (e.g. handle_start_charge_today_changed
        called from Ems.update_hourly which is sync). save_if_changed dispatches
        the persist via AsyncTaskRunner.run — caller does not block.
        """
        if changed:
            self._repo.save_if_changed()
            self._notify_all()

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to any state change. Returns unsubscribe callable."""
        self._listeners.append(cb)

        def _unsub() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(cb)

        return _unsub

    def _notify_all(self) -> None:
        for cb in self._listeners:
            cb()
