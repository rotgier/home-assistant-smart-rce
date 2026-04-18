"""Tests for calculate_target_soc() with optional consumption_profile."""

from __future__ import annotations

from custom_components.smart_rce.domain.pv_forecast import (
    CONSUMPTION_PER_30MIN,
    MIN_SOC_PERCENT,
    AdjustedPeriod,
    AdjustedPvForecast,
    ConsumptionProfile,
    calculate_target_soc,
)


def _make_forecast(rate_kwh_per_h: float) -> AdjustedPvForecast:
    """Build a constant-rate forecast 7:00-13:00 (12 periods of 30min).

    rate_kwh_per_h is an hourly rate; per 30min = rate/2.
    """
    periods = [
        AdjustedPeriod(
            period_start=f"2026-04-18T{hour:02d}:{minute:02d}:00+02:00",
            pv_estimate_adjusted=rate_kwh_per_h,
        )
        for hour in range(7, 13)
        for minute in (0, 30)
    ]
    total_kwh = (rate_kwh_per_h / 2) * len(periods)
    return AdjustedPvForecast(forecast=periods, total_kwh=total_kwh)


def test_surplus_pv_returns_min_soc() -> None:
    """Gdy PV pokrywa consumption, no deficit → MIN_SOC."""
    forecast = _make_forecast(2.0)  # PV 1.0 kWh/30min, > 0.45 consumption
    assert calculate_target_soc(forecast) == MIN_SOC_PERCENT


def test_no_profile_matches_constant_consumption() -> None:
    """Backward compat: consumption_profile=None behaves exactly like before."""
    forecast = _make_forecast(0.3)  # PV 0.15 kWh/30min, deficit vs 0.45
    without = calculate_target_soc(forecast)
    with_none = calculate_target_soc(forecast, consumption_profile=None)
    assert without == with_none
    # With PV << consumption there's accumulated deficit → SOC > MIN
    assert without > MIN_SOC_PERCENT


def test_profile_with_higher_consumption_raises_soc() -> None:
    """Profile sugerujący wyższe ranne consumption → wyższy target SOC."""
    forecast = _make_forecast(0.6)  # PV 0.3 kWh/30min
    # Higher morning consumption (7:00-9:30) than constant 0.45
    profile = ConsumptionProfile(
        buckets={
            (7, 0): 0.8,
            (7, 30): 0.8,
            (8, 0): 0.8,
            (8, 30): 0.8,
            (9, 0): 0.8,
            (9, 30): 0.5,
            # Later buckets: fallback to CONSUMPTION_PER_30MIN (0.45)
        }
    )
    baseline = calculate_target_soc(forecast)
    with_profile = calculate_target_soc(forecast, consumption_profile=profile)
    assert with_profile > baseline


def test_profile_with_lower_consumption_lowers_soc() -> None:
    """Profile sugerujący niższe consumption → niższy lub równy target SOC."""
    forecast = _make_forecast(0.6)
    profile = ConsumptionProfile(
        buckets={(h, m): 0.2 for h in range(7, 13) for m in (0, 30)}
    )
    baseline = calculate_target_soc(forecast)
    with_profile = calculate_target_soc(forecast, consumption_profile=profile)
    assert with_profile <= baseline


def test_partial_profile_falls_back_per_bucket() -> None:
    """Buckets z profile użyte, brakujące buckets → CONSUMPTION_PER_30MIN fallback."""
    forecast = _make_forecast(0.6)
    # Only bucket (7,0) overridden, rest falls back
    profile = ConsumptionProfile(buckets={(7, 0): 2.0})
    result = calculate_target_soc(forecast, consumption_profile=profile)
    # With one very high bucket we get at least the baseline deficit
    baseline = calculate_target_soc(forecast)
    assert result >= baseline


def test_empty_profile_behaves_like_none() -> None:
    forecast = _make_forecast(0.3)
    empty = ConsumptionProfile(buckets={})
    assert calculate_target_soc(
        forecast, consumption_profile=empty
    ) == calculate_target_soc(forecast)


def test_consumption_profile_get() -> None:
    p = ConsumptionProfile(buckets={(7, 0): 0.5, (7, 30): 0.6})
    assert p.get(7, 0) == 0.5
    assert p.get(7, 30) == 0.6
    assert p.get(8, 0) is None
    assert CONSUMPTION_PER_30MIN == 0.45  # invariant guard
