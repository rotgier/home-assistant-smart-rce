"""EnergyArchiveWriter: archives finished days once, skips days already archived."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from custom_components.smart_rce.infrastructure.daily_jsonl_archive import (
    DailyJsonlArchive,
)
from custom_components.smart_rce.infrastructure.energy_archive_writer import (
    EnergyArchiveWriter,
)

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_MODULE = "custom_components.smart_rce.infrastructure.energy_archive_writer"
_TODAY = date(2026, 10, 14)


def _fake_statistics(
    _hass: HomeAssistant,
    start: datetime,
    _end: datetime,
    statistic_ids: set[str],
    _period: str,
    _units: Any,
    _types: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """Two 5-min rows at 12:00 local for every id, on every day but 2026-10-10."""
    local_day = start.astimezone(dt_util.get_default_time_zone()).date()
    if local_day == date(2026, 10, 10):
        return {}
    noon = datetime.combine(
        local_day, time(12, 0), tzinfo=dt_util.get_default_time_zone()
    )
    rows = [
        {
            "start": (noon + timedelta(minutes=5 * i)).astimezone(UTC).timestamp(),
            "change": 0.1 * (i + 1),
            "mean": 50.0 + i,
        }
        for i in range(2)
    ]
    return dict.fromkeys(statistic_ids, rows)


async def test_catch_up_writes_missing_days_and_skips_archived(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    await hass.config.async_set_time_zone("Europe/Warsaw")
    archive = DailyJsonlArchive(tmp_path)
    archive.write_day(date(2026, 10, 13), [{"kept": True}])  # already archived
    recorder = MagicMock()
    recorder.async_add_executor_job = hass.async_add_executor_job

    with (
        patch(f"{_MODULE}.get_instance", return_value=recorder),
        patch(f"{_MODULE}.statistics_during_period", side_effect=_fake_statistics),
    ):
        written = await EnergyArchiveWriter(hass, archive).async_catch_up(_TODAY)

    # 9 complete days before today, minus the archived one and the empty one.
    assert date(2026, 10, 13) not in written
    assert date(2026, 10, 10) not in written
    assert written[0] == date(2026, 10, 5)
    assert len(written) == 7
    assert archive.read_day(date(2026, 10, 13)) == [{"kept": True}]

    record = archive.read_day(date(2026, 10, 12))[0]
    assert record["kind"] == "energy_day"
    assert record["series"]["pv_kwh"][144:146] == [0.1, 0.2]
    assert record["series"]["battery_soc_pct"][144:146] == [50.0, 51.0]
    assert set(record["series"]) == {
        "pv_kwh",
        "consumption_kwh",
        "consumption_minus_water_kwh",
        "grid_net_export_kwh",
        "battery_soc_pct",
    }
