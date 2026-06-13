"""Listenable — parameterless listener registry shared by application services.

HA entities subscribe via `add_listener(cb=self.async_write_ha_state)` to
refresh themselves on any state change inside a bounded context. Listeners are
parameterless — HA core dedupes state-equal writes, so a single fan-out per
object is enough.

Split out from `Service` so objects that need the registry but own no
repository (e.g. `MowingPlannerService`, a pure compute service) can mix it in
without faking a `_RepoProto`.
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib


class Listenable:
    """Parameterless listener registry: subscribe → refresh, returns unsubscribe."""

    def __init__(self) -> None:
        self._listeners: list[Callable[[], None]] = []

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
