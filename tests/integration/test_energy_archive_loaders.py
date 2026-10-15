"""Consumption loaders read the energy archive first, the recorder for the rest."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from custom_components.smart_rce.domain.energy_day import EnergyDay
from custom_components.smart_rce.infrastructure.daily_jsonl_archive import (
    DailyJsonlArchive,
)
from custom_components.smart_rce.infrastructure.energy_archive_reader import (
    EnergyArchiveReader,
)
from custom_components.smart_rce.infrastructure.pv_forecast.consumption_profile_loader import (
    ConsumptionProfileLoader,
)
from custom_components.smart_rce.infrastructure.pv_forecast.hourly_energy_loader import (
    HourlyEnergyLoader,
)
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_PKG = "custom_components.smart_rce.infrastructure.pv_forecast"
_ANCHOR = date(2026, 10, 16)  # Friday
_ARCHIVED = date(2026, 10, 15)
_NOT_ARCHIVED = date(2026, 10, 14)


def _archive_day(archive: DailyJsonlArchive, day: date, kwh: float) -> None:
    """Archive `day` with `kwh` per 5-min slot, 07:00-07:30 left incomplete."""
    tz = dt_util.get_default_time_zone()
    midnight = datetime.combine(day, time(0), tzinfo=tz)
    readings = {
        midnight + timedelta(minutes=5 * i): kwh for i in range(288) if i != 7 * 12
    }
    energy = EnergyDay.from_readings(day, tz, {"consumption_minus_water_kwh": readings})
    archive.write_day(day, [energy.to_dict()])


def _recorder(hass: HomeAssistant) -> MagicMock:
    recorder = MagicMock()
    recorder.async_add_executor_job = hass.async_add_executor_job
    return recorder


@pytest.fixture
async def archive(hass: HomeAssistant, tmp_path: Path) -> DailyJsonlArchive:
    await hass.config.async_set_time_zone("Europe/Warsaw")
    archive = DailyJsonlArchive(tmp_path)
    _archive_day(archive, _ARCHIVED, 0.02)
    return archive


async def test_profile_loader_takes_archived_days_and_asks_the_recorder_for_the_rest(
    hass: HomeAssistant, archive: DailyJsonlArchive
) -> None:
    tz = dt_util.get_default_time_zone()
    asked: list[date] = []

    def _stats(
        _hass: HomeAssistant,
        start: datetime,
        _end: datetime,
        statistic_ids: set[str],
        *_args: Any,
    ) -> dict[str, list[dict[str, Any]]]:
        day = start.astimezone(tz).date()
        asked.append(day)
        # Cycle totals of the utility meter at :25 / :55 (state).
        rows = [
            {
                "start": datetime.combine(
                    day, time(hour, minute), tzinfo=tz
                ).timestamp(),
                "state": 0.3,
            }
            for hour in range(7, 13)
            for minute in (25, 55)
        ]
        return dict.fromkeys(statistic_ids, rows)

    workdays = MagicMock()

    async def _fetch_workdays(_anchor: date) -> set[date]:
        return {_ARCHIVED, _NOT_ARCHIVED}

    workdays.fetch_workdays = _fetch_workdays
    loader = ConsumptionProfileLoader(
        hass, workdays, EnergyArchiveReader(hass, archive)
    )
    module = f"{_PKG}.consumption_profile_loader"
    with (
        patch(f"{module}.get_instance", return_value=_recorder(hass)),
        patch(f"{module}.statistics_during_period", side_effect=_stats),
    ):
        profiles = await loader.fetch_for_anchor(_ANCHOR, 3)

    archived, recorded, absent = profiles
    assert archived is not None
    assert archived.source_date == _ARCHIVED
    assert archived.get(7, 0) == 0.45  # incomplete half hour -> default
    assert archived.get(7, 30) == pytest.approx(0.12)
    assert archived.get(12, 30) == pytest.approx(0.12)
    assert recorded is not None
    assert recorded.source_date == _NOT_ARCHIVED
    assert recorded.get(9, 0) == 0.3
    assert absent is None  # only two workdays known
    assert asked == [_NOT_ARCHIVED]  # the archived day never hit the recorder


async def test_hourly_loader_takes_archived_slots_and_splits_lts_hours(
    hass: HomeAssistant, archive: DailyJsonlArchive
) -> None:
    tz = dt_util.get_default_time_zone()
    asked: list[tuple[date, str]] = []

    def _stats(
        _hass: HomeAssistant,
        start: datetime,
        _end: datetime,
        statistic_ids: set[str],
        period: str,
        *_args: Any,
    ) -> dict[str, list[dict[str, Any]]]:
        day = start.astimezone(tz).date()
        asked.append((day, period))
        rows = [
            {
                "start": datetime.combine(day, time(hour), tzinfo=tz).timestamp(),
                "change": 0.8,
            }
            for hour in range(24)
        ]
        return dict.fromkeys(statistic_ids, rows)

    loader = HourlyEnergyLoader(hass, EnergyArchiveReader(hass, archive))
    module = f"{_PKG}.hourly_energy_loader"
    with (
        patch(f"{module}.get_instance", return_value=_recorder(hass)),
        patch(f"{module}.statistics_during_period", side_effect=_stats),
    ):
        by_day = await loader.consumption_by_day([_ARCHIVED, _NOT_ARCHIVED])

    assert len(by_day[_ARCHIVED]) == 47  # 07:00-07:30 incomplete, left out
    assert by_day[_ARCHIVED][15] == pytest.approx(0.12)
    assert by_day[_NOT_ARCHIVED] == dict.fromkeys(range(48), 0.4)
    assert asked == [(_NOT_ARCHIVED, "hour")]
