"""Final day figures land as daily external statistics, and a rewrite replaces the row."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from typing import Any
from zoneinfo import ZoneInfo

from custom_components.smart_rce.domain.forecast_score.day_score import DayScore
from custom_components.smart_rce.infrastructure.forecast_score_statistics import (
    ForecastScoreStatistics,
    statistic_id,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant

TZ = ZoneInfo("Europe/Warsaw")
DAY = date(2026, 10, 14)


def _score(reserve_error: int) -> DayScore:
    """Build a minimal final day whose 06:11 production error is `reserve_error`."""
    record: dict[str, Any] = {
        "day": DAY.isoformat(),
        "status": "final",
        "computed_at": "2026-10-14T21:06:00+02:00",
        "reserve": [
            {
                "label": "06:11",
                "snapshot_at": "2026-10-14T06:10:50+02:00",
                "start_charge_hour": 11,
                "needed": 31,
                "needed_strict": 21.4,
                "needed_with_model": 27,
                "variants": [],
                "production": {
                    "setup": "test",
                    "needed": 31,
                    "needed_with_model": 27,
                    "asked": 31 + reserve_error,
                },
            }
        ],
        "reserve_models": [],
        "start": [],
        "outcome": {"start": {"source": "none"}, "topups": []},
    }
    return DayScore.from_dict(json.loads(json.dumps(record)))


async def test_rows_are_daily_and_rewritable(
    recorder_mock: Any, hass: HomeAssistant
) -> None:
    publisher = ForecastScoreStatistics(hass, lambda: TZ)
    publisher.publish([_score(-15)])
    await async_wait_recording_done(hass)
    publisher.publish([_score(-12)])  # the 00:20 check rewrites the day
    await async_wait_recording_done(hass)

    start = datetime(2026, 10, 14, tzinfo=TZ)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        start + timedelta(days=1),
        {statistic_id("reserve_error"), statistic_id("grid_topup")},
        "day",
        None,
        {"mean"},
    )
    rows = stats[statistic_id("reserve_error")]
    assert [r["mean"] for r in rows] == [-12.0]
    assert [r["mean"] for r in stats[statistic_id("grid_topup")]] == [0.0]
