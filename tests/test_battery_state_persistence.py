"""Tests for BatteryStatePersistence (application service in adapter)."""

import contextlib
from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock

from custom_components.smart_rce import adapter as adapter_module
from custom_components.smart_rce.adapter import BatteryStatePersistence
from custom_components.smart_rce.domain.battery import BatteryManager
from custom_components.smart_rce.domain.input_state import InputState
from custom_components.smart_rce.domain.rce import TIMEZONE
import pytest


def _state(
    *,
    now: datetime,
    exported_energy_hourly: float = 0.0,
    start_charge_hour_override: time | None = None,
    rce_should_hold_for_peak: bool | None = None,
    is_workday: bool | None = None,
    consumption_minus_pv_5_minutes: float | None = None,
) -> InputState:
    return InputState(
        battery_charge_limit=18.0,
        exported_energy_hourly=exported_energy_hourly,
        start_charge_hour_override=start_charge_hour_override,
        rce_should_hold_for_peak=rce_should_hold_for_peak,
        is_workday=is_workday,
        consumption_minus_pv_5_minutes=consumption_minus_pv_5_minutes,
        now=now,
    )


def _at(h: int, m: int = 0) -> datetime:
    return datetime(2026, 4, 20, h, m, tzinfo=TIMEZONE)  # Monday


@pytest.fixture
def mock_store(monkeypatch):
    """Replace Store class with MagicMock instance for capture."""
    store = MagicMock()
    store.async_save = AsyncMock()
    store.async_load = AsyncMock(return_value=None)
    monkeypatch.setattr(adapter_module, "Store", lambda *args, **kwargs: store)
    return store


@pytest.fixture
def mock_hass():
    """Minimal HA stub."""
    return MagicMock()


@pytest.fixture
def mock_entry():
    """Minimal entry stub — captures async_create_task targets and drains."""
    entry = MagicMock()
    captured: list = []

    def capture(_hass, coro, name=None):  # signature: (hass, target, name=...)
        captured.append(coro)
        with contextlib.suppress(StopIteration):
            coro.send(None)

    entry.async_create_task = MagicMock(side_effect=capture)
    entry._captured = captured
    return entry


@pytest.mark.asyncio
async def test_save_on_state_change(mock_hass, mock_entry, mock_store):
    mgr = BatteryManager()
    persistence = BatteryStatePersistence(mock_hass, mock_entry, mgr)
    await persistence.async_restore()  # initial snapshot capture

    # Trigger state change: afternoon-dynamic surplus → block_discharge=True
    mgr.update(
        _state(
            now=_at(14, 0),
            rce_should_hold_for_peak=False,
            consumption_minus_pv_5_minutes=-1000.0,
            exported_energy_hourly=0.5,
        )
    )
    persistence.save_if_changed()

    assert mgr.should_block_battery_discharge is True
    mock_store.async_save.assert_called_once()
    saved = mock_store.async_save.call_args.args[0]
    assert saved == {
        "block_discharge": True,
        "last_hour_seen": None,
    }


@pytest.mark.asyncio
async def test_no_save_when_state_unchanged(mock_hass, mock_entry, mock_store):
    mgr = BatteryManager()
    persistence = BatteryStatePersistence(mock_hass, mock_entry, mgr)
    await persistence.async_restore()

    # First update — sets block_discharge=True (state change)
    mgr.update(
        _state(
            now=_at(14, 0),
            rce_should_hold_for_peak=False,
            consumption_minus_pv_5_minutes=-1000.0,
            exported_energy_hourly=0.5,
        )
    )
    persistence.save_if_changed()
    mock_store.async_save.reset_mock()

    # Second update with same conditions — no state change
    mgr.update(
        _state(
            now=_at(14, 1),
            rce_should_hold_for_peak=False,
            consumption_minus_pv_5_minutes=-1000.0,
            exported_energy_hourly=0.5,
        )
    )
    persistence.save_if_changed()

    mock_store.async_save.assert_not_called()


@pytest.mark.asyncio
async def test_restore_loads_state(mock_hass, mock_entry, mock_store):
    mock_store.async_load.return_value = {
        "block_discharge": True,
        "last_hour_seen": 8,
    }
    mgr = BatteryManager()
    persistence = BatteryStatePersistence(mock_hass, mock_entry, mgr)

    await persistence.async_restore()

    assert mgr.should_block_battery_discharge is True
    assert mgr._last_hour_seen == 8


@pytest.mark.asyncio
async def test_restore_with_no_data_keeps_defaults(mock_hass, mock_entry, mock_store):
    mock_store.async_load.return_value = None
    mgr = BatteryManager()
    persistence = BatteryStatePersistence(mock_hass, mock_entry, mgr)

    await persistence.async_restore()

    assert mgr.should_block_battery_discharge is False
    assert mgr._last_hour_seen is None


def test_battery_manager_pure_no_hass_arg():
    """Domain BatteryManager nie przyjmuje hass — pure domain."""
    mgr = BatteryManager()
    mgr.update(
        _state(
            now=_at(14, 0),
            rce_should_hold_for_peak=False,
            consumption_minus_pv_5_minutes=-1000.0,
            exported_energy_hourly=0.5,
        )
    )
    assert mgr.should_block_battery_discharge is True


def test_snapshot_returns_current_state():
    mgr = BatteryManager()
    mgr.should_block_battery_discharge = True
    mgr._last_hour_seen = 7
    assert mgr.snapshot() == {"block_discharge": True, "last_hour_seen": 7}


def test_restore_applies_data_to_manager():
    mgr = BatteryManager()
    mgr.restore({"block_discharge": True, "last_hour_seen": 8})
    assert mgr.should_block_battery_discharge is True
    assert mgr._last_hour_seen == 8
