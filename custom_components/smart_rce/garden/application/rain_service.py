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
from custom_components.smart_rce.garden.domain.rain import RainEvent
from custom_components.smart_rce.garden.infrastructure.rain_repository import (
    RainRepository,
)
from homeassistant.core import callback

if TYPE_CHECKING:
    from datetime import datetime


class RainService(Service[RainRepository]):
    """Owns rain-end observation + dry-out policy; notifies entities + planner."""

    @callback
    def observe(self, raw_wet: bool, now: datetime) -> None:
        """Feed a raw wetness reading to the aggregate (sync, fire-and-forget).

        `raw_wet` is the instantaneous weather reading (`RainReader`); the
        aggregate confirms it only after `WET_DWELL` of sustained rain (a few
        drops never confirm). Persist AND notify on any observable `RainEvent`:
        `is_wet` + `last_wet_at` are persisted too (restart-mid-rain resilience),
        so we must save on `RAIN_CONFIRMED`/`STILL_RAINING`, not only on
        `RAIN_ENDED` — else a restart mid-rain restores a stale (dry) snapshot
        and the mower could resume into wet grass. Store writes are diff-guarded
        and local (cheap) — this is NOT the 300-sends/24h device budget.
        """
        event = self._repo.state.observe(raw_wet, now)
        if event is RainEvent.NONE:
            return
        self._repo.save_if_changed()
        self._notify_all()

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
        """Confirmed wet state — raw rain sustained past WET_DWELL."""
        return self._repo.state.is_wet
