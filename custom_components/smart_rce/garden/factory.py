"""Garden composition root — wires the garden bounded context.

Modular per ADR-024: garden owns its factory (unlike `ems_factory` at repo root,
since ems is still flat). v1 wires non-work ownership only — repo (restored) +
service, plus a listener on the mammotion non_work sensor that feeds
`NonWorkService.update_cloud_state` (drift detection; observe-first — no seed,
no automatic writes; `NonWorkActuator` fires only via the user's dashboard
push button until phase 2). The listener
fires whenever the sensor changes, so it works regardless of mammotion's
startup timing. Mowing planner (2b): `MowingPlannerService` pulls telemetry
(LubaStateReader), forecast (ems-published HourlyForecastProvider — handed in
by `async_setup_entry`, factory-level integration via an application Protocol)
and the non-work target; recomputes on input changes plus a 1-minute tick.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from custom_components.smart_rce.garden.application.mowing_planner_service import (
    MowingPlannerService,
)
from custom_components.smart_rce.garden.application.non_work_service import (
    NonWorkService,
)
from custom_components.smart_rce.garden.application.rain_service import RainService
from custom_components.smart_rce.garden.infrastructure.forecast_reader import (
    ForecastReader,
)
from custom_components.smart_rce.garden.infrastructure.luba_state_reader import (
    LubaStateReader,
)
from custom_components.smart_rce.garden.infrastructure.non_work_actuator import (
    NonWorkActuator,
)
from custom_components.smart_rce.garden.infrastructure.non_work_reader import (
    NonWorkReader,
)
from custom_components.smart_rce.garden.infrastructure.non_work_repository import (
    NonWorkRepository,
)
from custom_components.smart_rce.garden.infrastructure.rain_reader import RainReader
from custom_components.smart_rce.garden.infrastructure.rain_repository import (
    RainRepository,
)
from custom_components.smart_rce.infrastructure.async_task_runner import AsyncTaskRunner
from homeassistant.core import CoreState, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from datetime import datetime

    from custom_components.smart_rce.application.hourly_forecast import (
        HourlyForecastProvider,
    )
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_PLANNER_TICK = timedelta(minutes=1)
_RAIN_TICK = timedelta(minutes=5)


@dataclass
class Garden:
    """Garden bounded context — public services exposed to platforms."""

    non_work: NonWorkService
    rain: RainService
    mowing: MowingPlannerService


async def create_garden(
    hass: HomeAssistant, entry: ConfigEntry, forecast: HourlyForecastProvider
) -> Garden:
    """Wire the garden context (call from async_setup_entry before runtime_data)."""
    tasks = AsyncTaskRunner(hass, entry)
    repo = NonWorkRepository(hass, tasks)
    await repo.async_restore()
    reader = NonWorkReader(hass)
    service = NonWorkService(repo, NonWorkActuator(hass, repo))
    _wire_non_work_cloud_listener(hass, entry, reader, service)

    rain_repo = RainRepository(hass, tasks)
    await rain_repo.async_restore()
    rain = RainService(rain_repo)
    _wire_rain(hass, entry, RainReader(hass), rain)

    luba = LubaStateReader(hass)
    forecast_reader = ForecastReader(forecast)
    mowing = MowingPlannerService(luba, forecast_reader, service, rain, dt_util.now)
    _wire_mowing_recompute(hass, entry, luba, forecast_reader, service, rain, mowing)
    return Garden(non_work=service, rain=rain, mowing=mowing)


def _wire_rain(
    hass: HomeAssistant,
    entry: ConfigEntry,
    reader: RainReader,
    rain: RainService,
) -> None:
    """Observe rain on weather changes + a 5-min tick (wet→dry edge detection)."""

    @callback
    def _observe() -> None:
        rain.observe(reader.is_raining_now(), dt_util.now())

    @callback
    def _tick(_now: datetime) -> None:
        _observe()

    entry.async_on_unload(reader.subscribe(_observe))
    entry.async_on_unload(async_track_time_interval(hass, _tick, _RAIN_TICK))
    if hass.state is CoreState.running:
        _observe()


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


def _wire_mowing_recompute(
    hass: HomeAssistant,
    entry: ConfigEntry,
    luba: LubaStateReader,
    forecast: ForecastReader,
    non_work: NonWorkService,
    rain: RainService,
    mowing: MowingPlannerService,
) -> None:
    """Recompute on every input change + a 1-minute tick (time is an input)."""
    entry.async_on_unload(luba.subscribe(mowing.recompute))
    entry.async_on_unload(forecast.subscribe(mowing.recompute))
    entry.async_on_unload(non_work.add_listener(mowing.recompute))
    entry.async_on_unload(rain.add_listener(mowing.recompute))

    @callback
    def _tick(_now: datetime) -> None:
        # Must be @callback: a plain function is a JobType.Executor and HA runs
        # it in a worker thread, where recompute()'s async_write_ha_state is
        # thread-unsafe (raises in current HA).
        mowing.recompute()

    entry.async_on_unload(async_track_time_interval(hass, _tick, _PLANNER_TICK))
    if hass.state is CoreState.running:
        mowing.recompute()
