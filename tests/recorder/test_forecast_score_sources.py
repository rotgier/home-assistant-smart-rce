"""The scoreboard reads the morning plan with its attribute-only changes."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from custom_components.smart_rce.domain.forecast_score.full_charge_score import (
    decided_plan,
)
from custom_components.smart_rce.infrastructure.forecast_score_sources import (
    CONDITION_ENTITY,
    PLAN_ENTITY,
    RESERVE_ENTITY,
    WEATHER_FORECAST_ENTITY,
    ForecastScoreSources,
)
from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.core import HomeAssistant

TZ = ZoneInfo("Europe/Warsaw")
DAY = date(2026, 10, 19)  # a Monday


def _plan(full_at: str, *, day_off: bool = False) -> dict[str, Any]:
    return {
        "day": DAY.isoformat(),
        "hours": ["07:00", "08:00"],
        "full_at": full_at,
        "day_off": day_off,
    }


def _sources(hass: HomeAssistant) -> ForecastScoreSources:
    return ForecastScoreSources(
        hass,
        forecast_archive=MagicMock(),
        energy_archive=MagicMock(),
        weather_archive=MagicMock(),
        workday_loader=MagicMock(),
        same_type_loader=MagicMock(),
        rce_api=MagicMock(),
        ems=MagicMock(),
    )


async def test_the_plan_that_decided_is_the_last_one_before_its_first_hour(
    recorder_mock: Any, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The plan's state stays put while `full_at` moves — the later one counts.

    18.10.2026 the reader took the 00:00 plan (full at 12:54) instead of the
    04:41 one (12:56): it saw only rows where the state itself changed.
    """
    await hass.config.async_set_time_zone("Europe/Warsaw")
    steps = [
        (
            datetime(2026, 10, 19, 0, 0, 30, tzinfo=TZ),
            PLAN_ENTITY,
            "20",
            _plan("12:54"),
        ),
        (
            datetime(2026, 10, 19, 0, 0, 30, tzinfo=TZ),
            RESERVE_ENTITY,
            "40",
            {
                "value_without_margin": 25,
            },
        ),
        (datetime(2026, 10, 19, 4, 41, tzinfo=TZ), PLAN_ENTITY, "20", _plan("12:56")),
        (
            datetime(2026, 10, 19, 6, 30, tzinfo=TZ),
            RESERVE_ENTITY,
            "40",
            {
                "value_without_margin": 27,
            },
        ),
        # From the first hour on the plan no longer decides.
        (datetime(2026, 10, 19, 7, 5, tzinfo=TZ), PLAN_ENTITY, "20", _plan("13:30")),
    ]
    for moment, entity, state, attrs in steps:
        freezer.move_to(moment)
        hass.states.async_set(entity, state, attrs)
        await hass.async_block_till_done()
    freezer.move_to(datetime(2026, 10, 19, 9, 0, tzinfo=TZ))
    await async_wait_recording_done(hass)

    readings = await _sources(hass).morning_plan_readings(DAY)
    plan = decided_plan(readings, DAY)

    assert plan is not None
    assert plan.at == datetime(2026, 10, 19, 4, 41, tzinfo=TZ)
    assert plan.full_at == 12 * 60 + 56
    assert plan.hours == (7, 8)
    # The reserve's attribute-only change at 06:30 is after the plan: not it.
    assert plan.start_soc == 25.0
    # Every reading of the day, each with the reserve in force then.
    assert [r.full_at for r in readings] == [12 * 60 + 54, 12 * 60 + 56, 13 * 60 + 30]
    assert readings[-1].start_soc == 27.0


async def test_the_forecast_at_a_moment_is_the_last_one_up_to_it(
    recorder_mock: Any, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """31.10.2026 09:41 — the 09:40 forecast, not the 09:45 one after it."""
    await hass.config.async_set_time_zone("Europe/Warsaw")

    def forecast(cond_12: str) -> dict[str, Any]:
        return {
            "forecast": [
                {"datetime": "2026-10-19T12:00:00+02:00", "condition_custom": cond_12}
            ]
        }

    steps = [
        (datetime(2026, 10, 19, 0, 0, 30, tzinfo=TZ), CONDITION_ENTITY, "rainy", {}),
        (datetime(2026, 10, 19, 9, 10, tzinfo=TZ), CONDITION_ENTITY, "cloudy", {}),
        (
            datetime(2026, 10, 19, 9, 35, tzinfo=TZ),
            WEATHER_FORECAST_ENTITY,
            "x",
            forecast("rainy"),
        ),
        (
            datetime(2026, 10, 19, 9, 40, tzinfo=TZ),
            WEATHER_FORECAST_ENTITY,
            "x",
            forecast("cloudy"),
        ),
        (
            datetime(2026, 10, 19, 9, 45, tzinfo=TZ),
            WEATHER_FORECAST_ENTITY,
            "x",
            forecast("partlycloudy"),
        ),
        (datetime(2026, 10, 19, 9, 50, tzinfo=TZ), CONDITION_ENTITY, "sunny", {}),
    ]
    for moment, entity, state, attrs in steps:
        freezer.move_to(moment)
        hass.states.async_set(entity, state, attrs)
        await hass.async_block_till_done()
    freezer.move_to(datetime(2026, 10, 19, 12, 0, tzinfo=TZ))
    await async_wait_recording_done(hass)

    weather = await _sources(hass).recorder_weather(DAY)

    assert weather is not None
    assert [c for _w, c in weather.observed] == ["rainy", "cloudy", "sunny"]
    assert [c for _w, c in weather.forecasts["09:41"].hourly] == ["cloudy"]
    points = weather.points("09:41", datetime(2026, 10, 19, 9, 41, tzinfo=TZ))
    assert [c for _w, c in points] == ["rainy", "cloudy", "cloudy"]


async def test_the_dear_day_choice_is_the_last_one_for_the_day(
    recorder_mock: Any, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The plan carries the choice in `dear_day`; yesterday's one does not count."""
    await hass.config.async_set_time_zone("Europe/Warsaw")
    steps = [
        (
            datetime(2026, 10, 19, 0, 0, 30, tzinfo=TZ),
            {"dear_day": {"day": "2026-10-18", "chosen": "full"}},
        ),
        (
            datetime(2026, 10, 19, 1, 0, tzinfo=TZ),
            {"dear_day": {"day": "2026-10-19", "chosen": "full"}},
        ),
        (
            datetime(2026, 10, 19, 4, 0, tzinfo=TZ),
            {"dear_day": {"day": "2026-10-19", "chosen": "normal"}},
        ),
    ]
    for moment, attrs in steps:
        freezer.move_to(moment)
        hass.states.async_set(PLAN_ENTITY, "20", attrs)
        await hass.async_block_till_done()
    freezer.move_to(datetime(2026, 10, 19, 9, 0, tzinfo=TZ))
    await async_wait_recording_done(hass)

    assert await _sources(hass).dear_day_choice(DAY) == "normal"
