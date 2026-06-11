"""Garden composition root — wires the garden bounded context.

Modular per ADR-024: garden owns its factory (unlike `ems_factory` at repo root,
since ems is still flat). v1 wires non-work ownership only — repo (restored) +
actuator + service, plus a listener on the mammotion non_work sensor that drives
`NonWorkService.reconcile_from_cloud` (seed when unset, reassert on drift). The
listener fixes the startup race: it fires whenever the sensor becomes available,
not at a fixed `EVENT_HOMEASSISTANT_STARTED` moment. Mowing planner (2b) joins here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from custom_components.smart_rce.garden.application.non_work_service import (
    NonWorkService,
)
from custom_components.smart_rce.garden.const import (
    LUBA_LAWN_MOWER,
    LUBA_NON_WORK_SENSOR,
)
from custom_components.smart_rce.garden.infrastructure.non_work_actuator import (
    NonWorkActuator,
)
from custom_components.smart_rce.garden.infrastructure.non_work_reader import (
    read_non_work_hours,
)
from custom_components.smart_rce.garden.infrastructure.non_work_repository import (
    NonWorkRepository,
)
from custom_components.smart_rce.infrastructure.async_task_runner import AsyncTaskRunner
from homeassistant.core import CoreState, callback
from homeassistant.helpers.event import async_track_state_change_event

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import Event, HomeAssistant
    from homeassistant.helpers.event import EventStateChangedData


@dataclass
class Garden:
    """Garden bounded context — public services exposed to platforms."""

    non_work: NonWorkService


async def create_garden(hass: HomeAssistant, entry: ConfigEntry) -> Garden:
    """Wire the garden context (call from async_setup_entry before runtime_data)."""
    tasks = AsyncTaskRunner(hass, entry)
    repo = NonWorkRepository(hass, tasks)
    await repo.async_restore()
    actuator = NonWorkActuator(
        hass, repo, sensor_id=LUBA_NON_WORK_SENSOR, mower_id=LUBA_LAWN_MOWER
    )
    service = NonWorkService(repo, actuator)
    _wire_non_work_reconcile(hass, entry, tasks, service)
    return Garden(non_work=service)


def _wire_non_work_reconcile(
    hass: HomeAssistant,
    entry: ConfigEntry,
    tasks: AsyncTaskRunner,
    service: NonWorkService,
) -> None:
    @callback
    def _reconcile() -> None:
        tasks.run_background(
            service.reconcile_from_cloud(
                read_non_work_hours(hass, LUBA_NON_WORK_SENSOR)
            ),
            name="smart_rce_garden_non_work_reconcile",
        )

    @callback
    def _on_sensor_change(_event: Event[EventStateChangedData]) -> None:
        _reconcile()

    entry.async_on_unload(
        async_track_state_change_event(hass, [LUBA_NON_WORK_SENSOR], _on_sensor_change)
    )
    # Reload scenario: sensor may already be available (no future state-change
    # event), so reconcile once now. Fresh HA start: the listener catches
    # mammotion's load (unavailable → value).
    if hass.state is CoreState.running:
        _reconcile()
