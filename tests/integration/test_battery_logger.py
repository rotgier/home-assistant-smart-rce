"""Integration tests for BatteryManagerLogger (logging extraction).

Domain `BatteryManager` jest pure (zero `_LOGGER`); logging trzymane
w `BatteryManagerLogger` w `adapter.py`. Te testy walidują że INFO
transitions, DEBUG snapshots i restored state pojawiają się w logach
po `ems.update_state` triggers.

Wzorzec inspirowany `test_grid_export_actuator.py::test_apply_logs_info_on_success`.
"""

from datetime import datetime
import logging
from typing import Any
from unittest.mock import AsyncMock

from custom_components.smart_rce.domain.rce import TIMEZONE
from custom_components.smart_rce.infrastructure.battery_persistence import (
    BATTERY_STORAGE_KEY,
)
from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import HomeAssistant

from . import init_integration

# Conditions które wymuszą block_discharge=True przez BatteryManager
# (afternoon-dynamic 14:00 + workday + hold_for_peak=False + sustained surplus).
BLOCK_DISCHARGE_INPUTS: dict[str, str] = {
    "sensor.house_consumption_minus_heaters_minus_pv_avg_5_minutes": "-1000",
    "sensor.total_export_import_hourly": "0.5",
    "binary_sensor.rce_should_hold_for_peak": "off",
    "binary_sensor.workday": "on",
}

# Conditions które przełączą block_discharge=True → False (deficit przy
# afternoon-dynamic — instant_deficit AND NOT hourly_net_export).
ALLOW_DISCHARGE_INPUTS: dict[str, str] = {
    "sensor.house_consumption_minus_heaters_minus_pv_avg_5_minutes": "500",
    "sensor.total_export_import_hourly": "-0.1",
    "binary_sensor.rce_should_hold_for_peak": "off",
    "binary_sensor.workday": "on",
}


async def test_logs_info_on_block_discharge_transition(
    hass: HomeAssistant,
    mock_rce_api: AsyncMock,
    set_smart_rce_inputs,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Block_discharge False→True → INFO log z reason (phase)."""
    freezer.move_to(datetime(2026, 5, 4, 14, 0, 0, tzinfo=TIMEZONE))
    set_smart_rce_inputs(ALLOW_DISCHARGE_INPUTS)
    await init_integration(hass)
    await hass.async_block_till_done()

    with caplog.at_level(logging.INFO, logger="custom_components.smart_rce.adapter"):
        # Drive transition False → True (sustained surplus + hourly export)
        set_smart_rce_inputs(BLOCK_DISCHARGE_INPUTS)
        await hass.async_block_till_done()

    transition_logs = [
        r.message
        for r in caplog.records
        if "BatteryManager: block_discharge" in r.message
    ]
    assert any(
        "False → True" in m for m in transition_logs
    ), f"Expected transition INFO; got: {transition_logs}"
    assert any(
        "afternoon-dynamic" in m for m in transition_logs
    ), f"Expected reason 'afternoon-dynamic'; got: {transition_logs}"


async def test_logs_throttled_debug_snapshot(
    hass: HomeAssistant,
    mock_rce_api: AsyncMock,
    set_smart_rce_inputs,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pierwszy update emituje DEBUG snapshot z phase + key fields."""
    freezer.move_to(datetime(2026, 5, 4, 14, 0, 0, tzinfo=TIMEZONE))
    set_smart_rce_inputs(BLOCK_DISCHARGE_INPUTS)

    with caplog.at_level(logging.DEBUG, logger="custom_components.smart_rce.adapter"):
        await init_integration(hass)
        await hass.async_block_till_done()

    debug_logs = [
        r.message for r in caplog.records if r.message.startswith("BatteryManager[")
    ]
    assert any(
        "[afternoon-dynamic]" in m for m in debug_logs
    ), f"Expected DEBUG snapshot z phase=afternoon-dynamic; got: {debug_logs}"
    # Snapshot zawiera key fields
    assert any(
        "block_discharge=True" in m for m in debug_logs
    ), f"Expected block_discharge=True; got: {debug_logs}"


async def test_skips_log_when_none_present(
    hass: HomeAssistant,
    mock_rce_api: AsyncMock,
    set_smart_rce_inputs,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Brak exported_energy_hourly → DEBUG `skipped (none_present)`, no INFO."""
    freezer.move_to(datetime(2026, 5, 4, 14, 0, 0, tzinfo=TIMEZONE))
    set_smart_rce_inputs({"sensor.total_export_import_hourly": "unavailable"})

    with caplog.at_level(logging.DEBUG, logger="custom_components.smart_rce.adapter"):
        await init_integration(hass)
        await hass.async_block_till_done()

    skip_logs = [
        r.message for r in caplog.records if "skipped (none_present)" in r.message
    ]
    assert len(skip_logs) >= 1, (
        f"Expected DEBUG 'skipped (none_present)'; got logs: "
        f"{[r.message for r in caplog.records if 'BatteryManager' in r.message]}"
    )


async def test_logs_restored_info_when_storage_pre_populated(
    hass: HomeAssistant,
    mock_rce_api: AsyncMock,
    set_smart_rce_inputs,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pre-populated storage → 'BatteryManager restored' INFO przy first log_if_changed."""
    freezer.move_to(datetime(2026, 5, 4, 14, 0, 0, tzinfo=TIMEZONE))
    hass_storage[BATTERY_STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": BATTERY_STORAGE_KEY,
        "data": {"block_discharge": True, "last_hour_seen": 7},
    }
    set_smart_rce_inputs(BLOCK_DISCHARGE_INPUTS)

    with caplog.at_level(logging.INFO, logger="custom_components.smart_rce.adapter"):
        await init_integration(hass)
        await hass.async_block_till_done()

    restored_logs = [
        r.message for r in caplog.records if "BatteryManager restored" in r.message
    ]
    assert (
        len(restored_logs) == 1
    ), f"Expected exactly 1 'restored' INFO; got: {restored_logs}"
