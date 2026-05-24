"""Unit tests for WaterHeaterReservedPolicy."""

from datetime import datetime

from custom_components.smart_rce.domain.water_heater_reserved_policy import (
    ReservedMode,
    WaterHeaterReservedInput,
    WaterHeaterReservedPolicy,
)
import pytest

NOW = datetime(2026, 5, 23, 12, 0, 0)
EMPTY_INPUT = WaterHeaterReservedInput()


def test_default_values():
    policy = WaterHeaterReservedPolicy()
    assert policy.mode == ReservedMode.AUTO
    assert policy.manual_value == 3000


def test_compute_current_value_auto_mode_returns_stub_constant():
    """AUTO mode: stub returns constant 3000 — independent of inputs."""
    policy = WaterHeaterReservedPolicy(mode=ReservedMode.AUTO)
    assert policy.compute_current_value(NOW, EMPTY_INPUT) == 3000
    assert (
        policy.compute_current_value(
            NOW,
            WaterHeaterReservedInput(
                rce_today=[0.1, 0.2, 0.3],
                pv_forecast_today=[1000, 2000, 3000],
                weather_summary="sunny",
            ),
        )
        == 3000
    )


def test_compute_current_value_manual_mode_returns_manual_value():
    """MANUAL mode: manual_value short-circuits the auto branch."""
    policy = WaterHeaterReservedPolicy(
        mode=ReservedMode.MANUAL,
        manual_value=2500,
    )
    assert policy.compute_current_value(NOW, EMPTY_INPUT) == 2500


def test_set_mode_idempotent():
    policy = WaterHeaterReservedPolicy(mode=ReservedMode.AUTO)
    assert policy.set_mode(ReservedMode.AUTO) is False
    assert policy.set_mode(ReservedMode.MANUAL) is True
    assert policy.mode == ReservedMode.MANUAL
    assert policy.set_mode(ReservedMode.MANUAL) is False


def test_set_manual_value_idempotent():
    policy = WaterHeaterReservedPolicy(manual_value=3000)
    assert policy.set_manual_value(3000) is False
    assert policy.set_manual_value(4500) is True
    assert policy.manual_value == 4500
    assert policy.set_manual_value(4500) is False


def test_to_dict_from_dict_roundtrip_auto():
    policy = WaterHeaterReservedPolicy(
        mode=ReservedMode.AUTO,
        manual_value=2500,
    )
    restored = WaterHeaterReservedPolicy.from_dict(policy.to_dict())
    assert restored.mode == ReservedMode.AUTO
    assert restored.manual_value == 2500


def test_to_dict_from_dict_roundtrip_manual():
    policy = WaterHeaterReservedPolicy(
        mode=ReservedMode.MANUAL,
        manual_value=4200,
    )
    restored = WaterHeaterReservedPolicy.from_dict(policy.to_dict())
    assert restored.mode == ReservedMode.MANUAL
    assert restored.manual_value == 4200


def test_from_dict_tolerates_unknown_mode():
    """Defensive parsing — unknown mode value falls back to AUTO."""
    restored = WaterHeaterReservedPolicy.from_dict(
        {"mode": "INVALID_MODE", "manual_value": 1500}
    )
    assert restored.mode == ReservedMode.AUTO
    assert restored.manual_value == 1500


def test_from_dict_tolerates_missing_keys():
    """Defensive parsing — missing keys fall back to defaults."""
    restored = WaterHeaterReservedPolicy.from_dict({})
    assert restored.mode == ReservedMode.AUTO
    assert restored.manual_value == 3000


@pytest.mark.parametrize("value", [1000, 3000, 6000])
def test_manual_value_accepts_range(value):
    policy = WaterHeaterReservedPolicy()
    policy.set_manual_value(value)
    assert policy.manual_value == value
