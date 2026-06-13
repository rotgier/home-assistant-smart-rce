"""RainService — records rain-end transitions and exposes the dry-out time.

`observe(currently_wet, now)` is fed by the rain reader (weather changes + a
periodic tick) and delegated to `RainState.observe`, which detects the wet→dry
edge and stamps `rain_ended_at = now` (persisted) — the dry-out clock starts
when rain ENDS, not while it falls.
`dry_at` (= rain_ended_at + dry_hours) is consumed by the planner to clamp its
mowing window. `set_dry_hours` is driven by the `number.garden_dry_out_hours`
entity (user-tunable policy).

Replaces the Jinja `luba_notify_mute_until` mechanism.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.smart_rce.application.service import Service
from custom_components.smart_rce.garden.infrastructure.rain_repository import (
    RainRepository,
)
from homeassistant.core import callback

if TYPE_CHECKING:
    from datetime import datetime


class RainService(Service[RainRepository]):
    """Owns rain-end observation + dry-out policy; notifies entities + planner."""

    @callback
    def observe(self, currently_wet: bool, now: datetime) -> None:
        """Feed a wetness observation to the aggregate (sync, fire-and-forget).

        Delegates the wet→dry edge detection to `RainState.observe`; persists
        (diff-guarded) and refreshes entities whenever anything observable
        changed — including a plain dry→wet onset, so the grass-wet sensor
        turns on immediately, not only on the eventual dry edge.
        """
        self._save_if_changed_and_notify(self._repo.state.observe(currently_wet, now))

    async def set_dry_hours(self, hours: float) -> None:
        await self._persist_and_notify(self._repo.state.set_dry_hours(hours))

    @property
    def dry_at(self) -> datetime | None:
        """When the grass is dry enough to mow (rain_ended_at + dry_hours)."""
        return self._repo.state.dry_at

    @property
    def dry_hours(self) -> float:
        return self._repo.state.dry_hours

    @property
    def currently_wet(self) -> bool:
        """Last observed wet state (drives the grass-wet binary sensor)."""
        return self._repo.state.is_wet
