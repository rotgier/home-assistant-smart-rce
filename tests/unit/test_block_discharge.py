"""Tests for block_discharge pure hysteresis functions.

Three pure fn (block_pre_charge, block_post_charge, block_afternoon_dynamic)
take (state, prev_block) → bool. No phase awareness — phase ownership lives
in DodPolicy. These tests exercise hysteresis algorithms in isolation.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.smart_rce.domain.block_discharge import (
    DISCHARGE_HYSTERESIS_RESET_WH,
    DISCHARGE_HYSTERESIS_SET_WH,
    PV_AVAIL_5MIN_DEFICIT_W,
    PV_AVAIL_5MIN_SURPLUS_W,
    block_afternoon_dynamic,
    block_post_charge,
    block_pre_charge,
)
from custom_components.smart_rce.domain.input_state import InputState
from custom_components.smart_rce.domain.rce import TIMEZONE
import pytest


def _state(
    *,
    exported_energy_hourly: float | None = 0.0,
    pv_available_5min: float | None = None,
) -> InputState:
    """Minimal InputState for block_discharge tests.

    Only exported_energy_hourly + pv_available_5min matter; rest defaulted.
    pv_available_5min stored as -consumption_minus_pv_5_minutes (negation).
    """
    return InputState(
        water_heater_big_is_on=False,
        water_heater_small_is_on=False,
        battery_soc=50.0,
        battery_charge_limit=18.0,
        battery_power_2_minutes=0.0,
        consumption_minus_pv_2_minutes=0.0,
        consumption_minus_pv_5_minutes=(
            -pv_available_5min if pv_available_5min is not None else None
        ),
        exported_energy_hourly=exported_energy_hourly,
        heater_mode="BALANCED",
        depth_of_discharge=None,
        rce_should_hold_for_peak=False,
        is_workday=True,
        now=datetime(2026, 4, 20, 8, 30, tzinfo=TIMEZONE),
    )


# --- block_pre_charge --- #


class TestBlockPreCharge:
    """Hysteresis MINE 100/50 + instant_surplus extension."""

    @pytest.mark.parametrize(
        ("exported_kwh", "pv_w", "prev", "expected"),
        [
            # SET: exported >= 100 Wh — sustained export, regardless of pv
            (0.150, None, True, True),
            (0.100, 0.0, False, True),
            # SET threshold is >= so 100 Wh exactly triggers
            (0.100, 200.0, False, True),
            # Forced reset: exported < 0 (hourly net import — NEGATIVE may handle)
            (-0.001, 800.0, True, False),  # surplus, but forced reset wins
            (-0.010, None, True, False),
            # Default reset: exported < 50 AND no instant_surplus
            (0.030, None, True, False),
            (0.030, 200.0, True, False),  # pv in dead zone, not surplus
            (0.000, None, True, False),
            # Extended keep-state: exported < 50 AND instant_surplus → keep
            (0.030, 700.0, True, True),
            (0.005, 800.0, True, True),  # hour-boundary scenario
            (0.030, 700.0, False, False),  # initial False stays False (no SET)
            # Dead zone (50..100 Wh) keeps state regardless of pv
            (0.060, None, True, True),
            (0.060, None, False, False),
            (0.080, 200.0, True, True),
            (0.080, 200.0, False, False),
            # Reset boundary 50: exactly 50 → not <50 → keep state
            (0.050, None, True, True),
            (0.050, None, False, False),
            # SET boundary 100: 99 Wh dead zone (not >=100), 100 SETs
            (0.099, None, False, False),
            (0.100, None, False, True),
            # PV surplus boundary: 500 not > 500 → no surplus extension
            (0.030, 500.0, True, False),  # not >500 → default reset
            (0.030, 501.0, True, True),  # > 500 → keep
        ],
    )
    def test_hysteresis(
        self, exported_kwh: float, pv_w: float | None, prev: bool, expected: bool
    ):
        result = block_pre_charge(
            _state(exported_energy_hourly=exported_kwh, pv_available_5min=pv_w),
            prev_block=prev,
        )
        assert result is expected

    def test_exported_none_keeps_state(self):
        """Defensive: exported=None → return prev_block (sensor missing)."""
        assert (
            block_pre_charge(
                _state(exported_energy_hourly=None, pv_available_5min=800.0),
                prev_block=True,
            )
            is True
        )
        assert (
            block_pre_charge(
                _state(exported_energy_hourly=None, pv_available_5min=None),
                prev_block=False,
            )
            is False
        )


# --- block_post_charge --- #


class TestBlockPostCharge:
    """Dual-trigger: instant_surplus OR hourly_set; instant_deficit AND hourly_reset."""

    @pytest.mark.parametrize(
        ("exported_kwh", "pv_w", "prev", "expected"),
        [
            # SET via instant_surplus alone (any hourly)
            (0.000, 700.0, False, True),
            (0.030, 800.0, False, True),
            (-0.020, 700.0, False, True),  # net import OK if instant surplus
            # SET via hourly alone (instant deficit OR dead zone OR None)
            (0.100, -100.0, False, True),  # hourly SET overrides instant deficit
            (0.150, 200.0, False, True),  # dead zone instant + hourly SET
            (0.100, None, False, True),  # pv=None + hourly SET
            # RESET: instant_deficit AND hourly_reset
            (0.030, -100.0, True, False),
            (0.000, -200.0, True, False),
            (0.040, -50.0, True, False),
            # Keep: dead zone instant (0..500) + hourly dead zone (50..100)
            (0.060, 200.0, True, True),
            (0.080, 300.0, False, False),
            # Keep: instant_deficit BUT hourly in dead zone
            (0.060, -100.0, True, True),
            (0.080, -100.0, False, False),
            # Keep: instant dead zone + hourly < 50 (no instant_deficit to RESET)
            (0.030, 200.0, True, True),
            (0.040, 100.0, False, False),
            # Keep: pv=None + hourly in dead zone
            (0.060, None, True, True),
            (0.060, None, False, False),
            # Keep: pv=None + hourly < 50 (need instant_deficit to RESET)
            (0.030, None, True, True),
            # Keep: instant_surplus boundary 500 not > 500 → no instant signal
            (0.060, 500.0, True, True),
            (0.060, 500.0, False, False),
            # SET: instant_surplus boundary 501
            (0.060, 501.0, False, True),
            # Keep: instant_deficit boundary 0 not < 0 → no instant signal
            (0.030, 0.0, True, True),
            (0.030, 0.0, False, False),
            # RESET: instant_deficit boundary -1 (< 0)
            (0.030, -1.0, True, False),
            # SET boundary 100 Wh hourly
            (0.100, 200.0, False, True),  # hourly SET (instant in dead zone)
            # Reset boundary 50 Wh: not <50 → no reset
            (0.050, -100.0, True, True),
        ],
    )
    def test_dual_trigger(
        self, exported_kwh: float, pv_w: float | None, prev: bool, expected: bool
    ):
        result = block_post_charge(
            _state(exported_energy_hourly=exported_kwh, pv_available_5min=pv_w),
            prev_block=prev,
        )
        assert result is expected

    def test_exported_none_keeps_state(self):
        assert (
            block_post_charge(
                _state(exported_energy_hourly=None, pv_available_5min=800.0),
                prev_block=True,
            )
            is True
        )
        assert (
            block_post_charge(
                _state(exported_energy_hourly=None, pv_available_5min=-200.0),
                prev_block=False,
            )
            is False
        )


# --- block_afternoon_dynamic --- #


class TestBlockAfternoonDynamic:
    """Aggressive thresholds (hourly > 0 SET, <= 0 + deficit RESET) — past PV peak."""

    @pytest.mark.parametrize(
        ("exported_kwh", "pv_w", "prev", "expected"),
        [
            # SET via instant_surplus alone
            (0.000, 700.0, False, True),
            (-0.050, 800.0, False, True),  # net import + surplus → still SET
            # SET via hourly_net_export (any positive)
            (0.001, -200.0, False, True),  # tiny export, deficit → still SET
            (0.030, None, False, True),
            (0.500, -1000.0, False, True),
            # RESET: instant_deficit AND not hourly_net_export
            (0.000, -100.0, True, False),
            (-0.030, -200.0, True, False),
            (0.000, -1.0, True, False),  # boundary deficit
            # Keep: dead zone instant (0..500) + no net export
            (0.000, 200.0, True, True),
            (0.000, 300.0, False, False),
            (-0.020, 200.0, True, True),  # net import + dead zone instant
            # SET wins: instant_deficit + hourly_net_export (positive) → SET via hourly
            (0.030, -100.0, False, True),
            # Keep: pv=None + no net export
            (0.000, None, True, True),
            (-0.010, None, False, False),
            # Boundaries
            (0.000, 500.0, True, True),  # surplus boundary 500 not > 500
            (0.000, 501.0, False, True),  # > 500
            (0.000, 0.0, True, True),  # deficit boundary 0 not < 0
            # Hourly boundary: 0 not > 0 → no SET via hourly
            (0.000, None, False, False),
            (0.001, None, False, True),  # > 0 → SET
        ],
    )
    def test_dual_trigger(
        self, exported_kwh: float, pv_w: float | None, prev: bool, expected: bool
    ):
        result = block_afternoon_dynamic(
            _state(exported_energy_hourly=exported_kwh, pv_available_5min=pv_w),
            prev_block=prev,
        )
        assert result is expected

    def test_exported_none_keeps_state(self):
        assert (
            block_afternoon_dynamic(
                _state(exported_energy_hourly=None, pv_available_5min=800.0),
                prev_block=True,
            )
            is True
        )
        assert (
            block_afternoon_dynamic(
                _state(exported_energy_hourly=None, pv_available_5min=-200.0),
                prev_block=False,
            )
            is False
        )


# --- thresholds sanity --- #


def test_threshold_constants():
    """Lock-in threshold values — change here = behavior change."""
    assert DISCHARGE_HYSTERESIS_SET_WH == 100
    assert DISCHARGE_HYSTERESIS_RESET_WH == 50
    assert PV_AVAIL_5MIN_SURPLUS_W == 500
    assert PV_AVAIL_5MIN_DEFICIT_W == 0
