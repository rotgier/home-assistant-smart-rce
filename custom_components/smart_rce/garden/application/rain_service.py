"""RainService — records rain-end transitions and exposes the dry-out time.

`observe(currently_wet, now)` is fed by the rain reader (weather changes + a
periodic tick). It detects the wet→dry edge and stamps `rain_ended_at = now`
(persisted) — the dry-out clock starts when rain ENDS, not while it falls.
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

    def __init__(self, repo: RainRepository) -> None:
        super().__init__(repo)
        self._was_wet = False

    @callback
    def observe(self, currently_wet: bool, now: datetime) -> None:
        """Record a wet→dry transition (sync, fire-and-forget persist).

        `_was_wet` starts False, so the first observation never fires a false
        transition: a dry first reading is a no-op; a wet first reading only
        arms `_was_wet` for the eventual dry edge.
        """
        if self._was_wet and not currently_wet:
            self._save_if_changed_and_notify(
                self._repo.state.record_dry_transition(now)
            )
        self._was_wet = currently_wet

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
        return self._was_wet
