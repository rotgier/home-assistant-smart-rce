"""Base for repository-backed application services — adds persist helpers.

Services mediate between repository-owned aggregates and HA entities. The
listener registry (entities subscribe to refresh on any state change) lives in
the `Listenable` mixin; `Service` adds the repository and persist-and-notify
helpers on top.

`_persist_and_notify(changed)` is the one-liner mutator helper — pass the
result of `policy.set_X(value)`. When the mutator returned True, persist
the aggregate via the repo and wake all subscribers.

`TRepo` generic param exposes typed `self._repo` to subclasses; bound to
the `Repository[T]` protocol (must have async `persist()`).
"""

from __future__ import annotations

from typing import Protocol

from homeassistant.core import callback

from .listenable import Listenable


class _RepoProto(Protocol):
    """Repository contract used by `Service` — persist + save_if_changed."""

    async def persist(self) -> None: ...

    def save_if_changed(self) -> None: ...


class Service[TRepo: _RepoProto](Listenable):
    """Repository-backed service — Listenable + persist-and-notify helpers."""

    def __init__(self, repo: TRepo) -> None:
        super().__init__()
        self._repo: TRepo = repo

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
