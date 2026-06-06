"""Tests for `pv_forecast_sensor` observability helpers (Phase C.1)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from custom_components.smart_rce.domain.bucket import Bucket
from custom_components.smart_rce.domain.pv_forecast import LivePvSignals, PvForecasts
from custom_components.smart_rce.sensor.pv_forecast_sensor import (
    _bucket_end_constant_kwh,
    _bucket_end_derivative_delta_kwh,
    _bucket_end_derivative_kwh,
    _effective_derivative,
)


# Field rename: catalog.signals uses shorter names than the previous TargetSocCatalog
# fields. Old → new mapping when migrating tests:
#   live_pv_power_w               → pv_power_w
#   pv_bucket_so_far_kwh          → bucket_so_far_kwh
#   live_pv_derivative_w_per_min  → derivative_w_per_min
#   pv_stability_stable           → stability_stable
def _catalog(
    *,
    live_pv_power_w: float | None = None,
    pv_bucket_so_far_kwh: float | None = None,
    live_pv_derivative_w_per_min: float | None = None,
    pv_stability_stable: bool | None = None,
) -> PvForecasts:
    """Build a PvForecasts with only live PV signals set (helpers' inputs)."""
    updater = PvForecasts()
    updater.live_pv_updated(
        LivePvSignals(
            pv_power_w=live_pv_power_w,
            bucket_so_far_kwh=pv_bucket_so_far_kwh,
            derivative_w_per_min=live_pv_derivative_w_per_min,
            stability_stable=pv_stability_stable,
        ),
        realized_pv_today={},
        consumption_w=None,
        start_charge_hour=None,
        now=datetime(2026, 1, 1, 12, 0),
    )
    return updater


_forecast = _catalog  # backwards-compat alias so legacy test bodies keep working


def test_effective_derivative_returns_value_when_stable_and_set() -> None:
    forecast = _forecast(pv_stability_stable=True, live_pv_derivative_w_per_min=90.0)
    assert _effective_derivative(forecast) == 90.0


def test_effective_derivative_zero_when_not_stable() -> None:
    forecast = _forecast(pv_stability_stable=False, live_pv_derivative_w_per_min=90.0)
    assert _effective_derivative(forecast) == 0.0


def test_effective_derivative_zero_when_stability_none() -> None:
    forecast = _forecast(pv_stability_stable=None, live_pv_derivative_w_per_min=90.0)
    assert _effective_derivative(forecast) == 0.0


def test_effective_derivative_zero_when_derivative_none() -> None:
    forecast = _forecast(pv_stability_stable=True, live_pv_derivative_w_per_min=None)
    assert _effective_derivative(forecast) == 0.0


def test_bucket_end_constant_kwh_none_when_live_pv_power_missing() -> None:
    forecast = _forecast(live_pv_power_w=None, pv_bucket_so_far_kwh=0.3)
    assert _bucket_end_constant_kwh(forecast) is None


def test_bucket_end_constant_kwh_none_when_so_far_missing() -> None:
    forecast = _forecast(live_pv_power_w=1500.0, pv_bucket_so_far_kwh=None)
    assert _bucket_end_constant_kwh(forecast) is None


def test_bucket_end_constant_kwh_matches_full_bucket_kwh() -> None:
    forecast = _forecast(live_pv_power_w=1500.0, pv_bucket_so_far_kwh=0.3)
    fixed_now = datetime(2026, 5, 15, 9, 13)
    with patch(
        "custom_components.smart_rce.sensor.pv_forecast_sensor.dt_util.now",
        return_value=fixed_now,
    ):
        actual = _bucket_end_constant_kwh(forecast)
    expected = Bucket.full_bucket_kwh(fixed_now, 1500.0, 0.3)
    assert actual == expected


def test_bucket_end_derivative_kwh_equals_constant_when_unstable() -> None:
    forecast = _forecast(
        live_pv_power_w=1500.0,
        pv_bucket_so_far_kwh=0.3,
        pv_stability_stable=False,
        live_pv_derivative_w_per_min=90.0,
    )
    fixed_now = datetime(2026, 5, 15, 9, 13)
    with patch(
        "custom_components.smart_rce.sensor.pv_forecast_sensor.dt_util.now",
        return_value=fixed_now,
    ):
        const_val = _bucket_end_constant_kwh(forecast)
        deriv_val = _bucket_end_derivative_kwh(forecast)
    assert deriv_val == const_val


def test_bucket_end_derivative_kwh_uses_ramp_when_stable() -> None:
    forecast = _forecast(
        live_pv_power_w=1500.0,
        pv_bucket_so_far_kwh=0.3,
        pv_stability_stable=True,
        live_pv_derivative_w_per_min=60.0,  # positive ramp
    )
    fixed_now = datetime(2026, 5, 15, 9, 13)
    with patch(
        "custom_components.smart_rce.sensor.pv_forecast_sensor.dt_util.now",
        return_value=fixed_now,
    ):
        const_val = _bucket_end_constant_kwh(forecast)
        deriv_val = _bucket_end_derivative_kwh(forecast)
    # Positive ramp adds energy on top of the constant baseline.
    assert deriv_val > const_val


def test_bucket_end_derivative_delta_kwh_zero_when_unstable() -> None:
    forecast = _forecast(
        live_pv_power_w=1500.0,
        pv_bucket_so_far_kwh=0.3,
        pv_stability_stable=False,
        live_pv_derivative_w_per_min=60.0,
    )
    with patch(
        "custom_components.smart_rce.sensor.pv_forecast_sensor.dt_util.now",
        return_value=datetime(2026, 5, 15, 9, 13),
    ):
        assert _bucket_end_derivative_delta_kwh(forecast) == 0.0


def test_bucket_end_derivative_delta_kwh_positive_when_ramp_active() -> None:
    forecast = _forecast(
        live_pv_power_w=1500.0,
        pv_bucket_so_far_kwh=0.3,
        pv_stability_stable=True,
        live_pv_derivative_w_per_min=60.0,
    )
    with patch(
        "custom_components.smart_rce.sensor.pv_forecast_sensor.dt_util.now",
        return_value=datetime(2026, 5, 15, 9, 13),
    ):
        assert _bucket_end_derivative_delta_kwh(forecast) > 0.0


def test_bucket_end_derivative_delta_kwh_none_when_signals_missing() -> None:
    forecast = _forecast(live_pv_power_w=None, pv_bucket_so_far_kwh=None)
    assert _bucket_end_derivative_delta_kwh(forecast) is None
