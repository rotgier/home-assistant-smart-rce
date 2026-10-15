"""Wires the forecast scoreboard: sources, archive, service, schedule.

16:06 preliminary, 21:06 final, 00:20 check of yesterday (after the energy
archive's 00:15), and a catch-up once HA has started (recorder up) — a little
later than the energy archive's own catch-up, which it reads.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, callback
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.util import dt as dt_util

from .application.forecast_score_service import ForecastScoreService
from .infrastructure.code_version import read_code_version
from .infrastructure.daily_jsonl_archive import DailyJsonlArchive
from .infrastructure.energy_archive_reader import (
    ENERGY_ARCHIVE_DIR,
    EnergyArchiveReader,
)
from .infrastructure.forecast_score_archive import ForecastScoreArchive
from .infrastructure.forecast_score_sources import ForecastScoreSources
from .infrastructure.forecast_score_statistics import ForecastScoreStatistics
from .infrastructure.pv_forecast.consumption_profile_loader import (
    ConsumptionProfileLoader,
)
from .infrastructure.pv_forecast.hourly_energy_loader import HourlyEnergyLoader
from .infrastructure.workday_calendar_reader import WorkdayCalendarReader
from .pv_forecast_factory import FORECAST_ARCHIVE_DIR

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .application.ems import Ems
    from .infrastructure.rce_api import RceApi

# After start: let the energy archive catch up first (it runs on start too).
_CATCH_UP_DELAY_SEC: Final = 120


async def create_forecast_score(
    hass: HomeAssistant, entry: ConfigEntry, ems: Ems, rce_api: RceApi
) -> ForecastScoreService:
    energy_reader = EnergyArchiveReader.in_config(hass)
    service = ForecastScoreService(
        hass,
        sources=ForecastScoreSources(
            hass,
            forecast_archive=DailyJsonlArchive(
                Path(hass.config.path(FORECAST_ARCHIVE_DIR))
            ),
            energy_archive=DailyJsonlArchive(
                Path(hass.config.path(ENERGY_ARCHIVE_DIR))
            ),
            workday_loader=ConsumptionProfileLoader(
                hass, WorkdayCalendarReader(hass), energy_reader
            ),
            same_type_loader=HourlyEnergyLoader(hass, energy_reader),
            rce_api=rce_api,
            ems=ems,
        ),
        archive=ForecastScoreArchive.in_config(hass),
        charge=ems.battery_charge_service,
        clock=dt_util.now,
        tz=dt_util.get_default_time_zone,
        code_version=await hass.async_add_executor_job(read_code_version),
        publish=ForecastScoreStatistics(hass, dt_util.get_default_time_zone).publish,
    )
    t = ForecastScoreService
    # Coroutine functions run on the event loop (a plain one would not).
    for run, at in (
        (service.async_preliminary, t.PRELIMINARY_AT),
        (service.async_final, t.FINAL_AT),
        (service.async_check, t.CHECK_AT),
    ):
        entry.async_on_unload(
            async_track_time_change(hass, run, hour=at.hour, minute=at.minute, second=0)
        )

    async def _catch_up(_now: datetime) -> None:
        await service.async_catch_up()

    @callback
    def _on_started(_event: Event | None) -> None:
        entry.async_on_unload(async_call_later(hass, _CATCH_UP_DELAY_SEC, _catch_up))

    if hass.is_running:
        _on_started(None)
    else:
        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)
        )
    return service
