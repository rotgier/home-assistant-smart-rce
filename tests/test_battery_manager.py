"""Tests for BatteryManager — hour balance tracking + should_block_battery_charge."""

from datetime import datetime

from custom_components.smart_rce.domain.battery import BatteryManager
from custom_components.smart_rce.domain.input_state import InputState
from custom_components.smart_rce.domain.rce import TIMEZONE

NOON = datetime(2026, 4, 16, 12, 0, tzinfo=TIMEZONE)
AFTER_GUARD = datetime(2026, 4, 16, 18, 0, tzinfo=TIMEZONE)  # hour >= 17


def _state(
    *,
    battery_charge_limit=18.0,
    exported_energy_hourly=0.5,
    depth_of_discharge=None,
    now=NOON,
) -> InputState:
    return InputState(
        # Pola potrzebne dla WaterHeaterManager, BatteryManager ignoruje je.
        water_heater_big_is_on=False,
        water_heater_small_is_on=False,
        battery_soc=50.0,
        battery_charge_limit=battery_charge_limit,
        battery_power_2_minutes=0.0,
        consumption_minus_pv_2_minutes=-3000.0,
        exported_energy_hourly=exported_energy_hourly,
        heater_mode="BALANCED",
        depth_of_discharge=depth_of_discharge,
        now=now,
    )


class TestGuardWindow:
    """DoD=0 + hour<17 → monitoruj bilans godzinowy."""

    def test_dod_zero_negative_export_sets_flag(self):
        mgr = BatteryManager()
        mgr.update(_state(depth_of_discharge=0, exported_energy_hourly=-0.05))
        assert mgr.hourly_balance_negative is True
        assert mgr.should_block_battery_charge is True

    def test_dod_zero_positive_export_flag_false(self):
        mgr = BatteryManager()
        mgr.update(_state(depth_of_discharge=0, exported_energy_hourly=0.5))
        assert mgr.hourly_balance_negative is False
        assert mgr.should_block_battery_charge is False

    def test_dod_nonzero_no_tracking(self):
        mgr = BatteryManager()
        mgr.update(_state(depth_of_discharge=90, exported_energy_hourly=-0.5))
        # Poza guardem (DoD!=0) — flag zawsze False
        assert mgr.hourly_balance_negative is False
        assert mgr.should_block_battery_charge is False

    def test_after_guard_hour_no_tracking(self):
        mgr = BatteryManager()
        mgr.update(
            _state(
                depth_of_discharge=0,
                exported_energy_hourly=-0.5,
                now=AFTER_GUARD,  # hour=18, poza guardem
            )
        )
        assert mgr.hourly_balance_negative is False
        assert mgr.should_block_battery_charge is False


class TestHysteresis:
    """Hysteresis 50 Wh — raz negative zostaje dopóki nie solidny export."""

    def test_hysteresis_keeps_negative(self):
        mgr = BatteryManager()
        # Najpierw ujemny bilans
        mgr.update(_state(depth_of_discharge=0, exported_energy_hourly=-0.01))
        assert mgr.hourly_balance_negative is True

        # Lekko dodatni +40 Wh (< 50 Wh threshold) — zostaje True
        mgr.update(_state(depth_of_discharge=0, exported_energy_hourly=0.04))
        assert mgr.hourly_balance_negative is True
        assert mgr.should_block_battery_charge is True

    def test_release_after_threshold(self):
        mgr = BatteryManager()
        mgr.update(_state(depth_of_discharge=0, exported_energy_hourly=-0.05))
        assert mgr.hourly_balance_negative is True

        # Solid export +100 Wh → release
        mgr.update(_state(depth_of_discharge=0, exported_energy_hourly=0.1))
        assert mgr.hourly_balance_negative is False
        assert mgr.should_block_battery_charge is False


class TestBatteryChargeLimit:
    """should_block_battery_charge wymaga charge_limit >= 2 A."""

    def test_low_charge_limit_no_block(self):
        mgr = BatteryManager()
        mgr.update(
            _state(
                depth_of_discharge=0,
                exported_energy_hourly=-0.1,
                battery_charge_limit=0,
            )
        )
        # hourly_balance_negative tak, ale block_charge NIE bo limit < 2
        assert mgr.hourly_balance_negative is True
        assert mgr.should_block_battery_charge is False

    def test_limit_2_block_active(self):
        mgr = BatteryManager()
        mgr.update(
            _state(
                depth_of_discharge=0,
                exported_energy_hourly=-0.1,
                battery_charge_limit=2,
            )
        )
        assert mgr.should_block_battery_charge is True


class TestNonePresent:
    """Brak wymaganych pól — update jest no-op."""

    def test_none_now_noop(self):
        mgr = BatteryManager()
        state = _state(depth_of_discharge=0, exported_energy_hourly=-0.1)
        state.now = None
        mgr.update(state)
        assert mgr.hourly_balance_negative is False

    def test_none_export_noop(self):
        mgr = BatteryManager()
        state = _state(depth_of_discharge=0)
        state.exported_energy_hourly = None
        mgr.update(state)
        assert mgr.hourly_balance_negative is False
