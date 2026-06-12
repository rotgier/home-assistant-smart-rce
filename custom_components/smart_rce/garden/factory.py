"""Garden composition root — wires the garden bounded context.

Modular per ADR-024: garden owns its factory (unlike `ems_factory` at repo root,
since ems is still flat). v1 wires non-work ownership only — repo (restored) +
service, plus a listener on the mammotion non_work sensor that feeds
`NonWorkService.update_cloud_state` (drift detection; observe-first — no seed,
no device writes; `NonWorkActuator` stays dormant until phase 2). The listener
fires whenever the sensor changes, so it works regardless of mammotion's
startup timing. Mowing planner (2b) joins here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from custom_components.smart_rce.garden.application.non_work_service import (
    NonWorkService,
)
from custom_components.smart_rce.garden.infrastructure.non_work_reader import (
    NonWorkReader,
)
from custom_components.smart_rce.garden.infrastructure.non_work_repository import (
    NonWorkRepository,
)
from custom_components.smart_rce.infrastructure.async_task_runner import AsyncTaskRunner
from homeassistant.core import CoreState, callback

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


@dataclass
class Garden:
    """Garden bounded context — public services exposed to platforms."""

    non_work: NonWorkService


async def create_garden(hass: HomeAssistant, entry: ConfigEntry) -> Garden:
    """Wire the garden context (call from async_setup_entry before runtime_data)."""
    tasks = AsyncTaskRunner(hass, entry)
    repo = NonWorkRepository(hass, tasks)
    await repo.async_restore()
    service = NonWorkService(repo)
    _wire_non_work_cloud_listener(hass, entry, NonWorkReader(hass), service)
    return Garden(non_work=service)


def _wire_non_work_cloud_listener(
    hass: HomeAssistant,
    entry: ConfigEntry,
    reader: NonWorkReader,
    service: NonWorkService,
) -> None:
    @callback
    def _update() -> None:
        service.update_cloud_state(reader.read_non_work_hours())

    entry.async_on_unload(reader.subscribe(_update))
    # Reload scenario: sensor may already be available (no future state-change
    # event), so read once now. Fresh HA start: the subscription catches
    # mammotion's load (unavailable → value).
    if hass.state is CoreState.running:
        _update()
