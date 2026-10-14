"""ForecastArchiveService on a real event loop: Solcast fetch → settle → one record."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from custom_components.smart_rce.application.forecast_archive_service import (
    ForecastArchiveService,
)
from custom_components.smart_rce.domain.pv_forecast import (
    LivePvSignals,
    PvForecast,
    PvForecasts,
    SolcastPeriod,
    WeatherConditionAtHour,
    WeatherConditions,
)
from custom_components.smart_rce.infrastructure.daily_jsonl_archive import (
    DailyJsonlArchive,
)
from custom_components.smart_rce.infrastructure.pv_forecast.solcast_reader import (
    SolcastReader,
)
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util


@pytest.fixture(autouse=True)
def _reset_bound_strategies():
    yield
    for variant in PvForecast:
        variant.strategy.result = None
        variant.strategy.remaining_kwh = None


async def _service(hass: HomeAssistant, root: Path) -> ForecastArchiveService:
    now = dt_util.now()
    forecasts = PvForecasts()
    periods = [
        SolcastPeriod(
            period_start=now.replace(
                hour=h, minute=m, second=0, microsecond=0
            ).isoformat(),
            pv_estimate=2.0,
            pv_estimate10=1.0,
            pv_estimate90=3.0,
        )
        for h in range(7, 13)
        for m in (0, 30)
    ]
    weather = WeatherConditions(
        conditions=[
            WeatherConditionAtHour(hour=h, condition_custom="sunny") for h in range(24)
        ]
    )
    forecasts.solcast_today_updated(periods, weather, now)
    forecasts.live_pv_updated(LivePvSignals(pv_power_w=1000.0), {}, None, None, now)
    solcast = SolcastReader(hass)
    service = ForecastArchiveService(
        hass,
        forecasts=forecasts,
        solcast=solcast,
        archive=DailyJsonlArchive(root),
        code_version="test",
    )
    async_track_state_change_event(
        hass, [solcast.api_update_entity_id], service.on_solcast_api_update
    )
    return service


async def _settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done()  # state_changed listeners run first
    # Fire the settle timer (and any other pending one) without waiting 20 s.
    async_fire_time_changed(hass, fire_all=True)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_fetch_writes_one_snapshot_restart_value_writes_none(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    await _service(hass, tmp_path)
    entity_id = SolcastReader(hass).api_update_entity_id
    archive = DailyJsonlArchive(tmp_path)
    today = dt_util.now().date()

    # Timestamps relative to the clock: a fetch is only accepted while its
    # "last API update" is recent (SnapshotSchedule.FETCH_MAX_AGE).
    restored = (dt_util.utcnow() - timedelta(hours=10)).isoformat()
    fetched = dt_util.utcnow().isoformat()

    # Sensor created at startup: an old value, not a fetch.
    hass.states.async_set(entity_id, restored)
    await _settle(hass)
    assert archive.read_day(today) == []

    # Real fetch.
    hass.states.async_set(entity_id, fetched)
    await _settle(hass)
    records = archive.read_day(today)
    assert len(records) == 1
    assert records[0]["trigger"] == "solcast"
    assert records[0]["solcast_fetched_at"] == fetched
    assert records[0]["variants"][PvForecast.LIVE.key]["result"] is not None

    # Same value again (attribute-only update): no second record.
    hass.states.async_set(entity_id, fetched, {"x": 1})
    await _settle(hass)
    assert len(archive.read_day(today)) == 1


async def test_resume_reads_back_what_was_written(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """After a restart the schedule starts from today's last record."""
    service = await _service(hass, tmp_path)
    entity_id = SolcastReader(hass).api_update_entity_id
    hass.states.async_set(entity_id, "v0")
    hass.states.async_set(entity_id, "v1")
    await _settle(hass)

    restarted = await _service(hass, tmp_path)
    await restarted.async_start()
    assert service is not restarted
    # The restarted service already knows "v1": re-emitting it writes nothing.
    hass.states.async_set(entity_id, "v1", {"restart": True})
    await _settle(hass)
    assert len(DailyJsonlArchive(tmp_path).read_day(dt_util.now().date())) == 1
