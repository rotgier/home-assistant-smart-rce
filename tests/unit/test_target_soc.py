"""Tests for `calculate_target_soc()` with required strict ConsumptionProfile.

Function lives in `domain/target_soc.py` (extracted from `pv_forecast.py`
to enable reuse by the target-SOC matrix). Constants + result dataclasses
moved alongside it. Input dataclasses (AdjustedPvForecast, AdjustedPeriod,
ConsumptionProfile) stay in `pv_forecast.py`.

After C0.5 ConsumptionProfile became a strict 12-bucket contract — see
`__post_init__` validation in `domain/pv_forecast.py`. Callers must
either pass a real profile or use `ConsumptionProfile.flat()` for the
constant baseline.
"""

from __future__ import annotations

from custom_components.smart_rce.domain.pv_forecast import (
    AdjustedPeriod,
    AdjustedPvForecast,
    ConsumptionProfile,
)
from custom_components.smart_rce.domain.target_soc import (
    CONSUMPTION_PER_30MIN,
    MIN_SOC_PERCENT,
    calculate_target_soc as _calculate_target_soc,
)
import pytest


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
    result = _calculate_target_soc(forecast, ConsumptionProfile.flat())
    assert result.value == MIN_SOC_PERCENT
    assert len(result.buckets) == 12  # pełne okno 7-13 (12 × 30min)


def test_flat_profile_matches_constant_consumption() -> None:
    """`ConsumptionProfile.flat()` reproduces the constant CONSUMPTION_PER_30MIN baseline."""
    forecast = _make_forecast(0.3)  # PV 0.15 kWh/30min, deficit vs 0.45
    result = _calculate_target_soc(forecast, ConsumptionProfile.flat())
    # With PV << consumption there's accumulated deficit → SOC > MIN
    assert result.value > MIN_SOC_PERCENT
    # Trace: consumption = const 0.45 for every bucket
    assert all(b.cons_kwh == CONSUMPTION_PER_30MIN for b in result.buckets)


def test_profile_with_higher_consumption_raises_soc() -> None:
    """Profile sugerujący wyższe ranne consumption → wyższy target SOC."""
    forecast = _make_forecast(0.6)  # PV 0.3 kWh/30min
    # 12 buckets — high morning + tapering down to baseline by noon.
    buckets = {(h, m): CONSUMPTION_PER_30MIN for h in range(7, 13) for m in (0, 30)}
    for slot in [(7, 0), (7, 30), (8, 0), (8, 30), (9, 0)]:
        buckets[slot] = 0.8
    buckets[(9, 30)] = 0.5
    profile = ConsumptionProfile(buckets=buckets)
    baseline = _calculate_target_soc(forecast, ConsumptionProfile.flat())
    with_profile = _calculate_target_soc(forecast, profile)
    assert with_profile.value > baseline.value


def test_profile_with_lower_consumption_lowers_soc() -> None:
    """Profile sugerujący niższe consumption → niższy lub równy target SOC."""
    forecast = _make_forecast(0.6)
    profile = ConsumptionProfile(
        buckets={(h, m): 0.2 for h in range(7, 13) for m in (0, 30)}
    )
    baseline = _calculate_target_soc(forecast, ConsumptionProfile.flat())
    with_profile = _calculate_target_soc(forecast, profile)
    assert with_profile.value <= baseline.value


def test_partial_profile_raises_validation_error() -> None:
    """Strict contract: partial buckets fail at construction, not at use."""
    with pytest.raises(ValueError, match="missing="):
        ConsumptionProfile(buckets={(7, 0): 2.0})


def test_empty_profile_raises_validation_error() -> None:
    """Empty buckets fail strict contract validation."""
    with pytest.raises(ValueError, match="missing="):
        ConsumptionProfile(buckets={})


def test_extra_buckets_raise_validation_error() -> None:
    """Buckets outside 7:00..12:30 are flagged as extra."""
    full = {(h, m): CONSUMPTION_PER_30MIN for h in range(7, 13) for m in (0, 30)}
    full[(13, 0)] = 0.5  # outside window
    with pytest.raises(ValueError, match="extra="):
        ConsumptionProfile(buckets=full)


def test_trace_has_is_min_flag() -> None:
    """Bucket z najbardziej ujemnym cumulative ma is_min=True (gdy jest deficit)."""
    forecast = _make_forecast(0.3)  # deficit przez całe okno
    result = _calculate_target_soc(forecast, ConsumptionProfile.flat())
    min_buckets = [b for b in result.buckets if b.is_min]
    assert len(min_buckets) == 1
    # Przy stałym deficycie — ostatni bucket ma największy (ujemny) cumulative
    assert min_buckets[0].period == "12:30"


def test_trace_contents() -> None:
    """Sprawdź że pojedynczy bucket trace ma oczekiwane pola."""
    forecast = _make_forecast(0.3)
    result = _calculate_target_soc(forecast, ConsumptionProfile.flat())
    b = result.buckets[0]
    assert b.period == "07:00"
    assert b.pv_kwh == 0.15
    assert b.cons_kwh == 0.45
    assert b.balance == round(0.15 - 0.45, 3)
    assert b.cumulative == b.balance
    b2 = result.buckets[1]
    assert b2.cumulative == round(2 * (0.15 - 0.45), 3)


def test_consumption_profile_get_returns_float() -> None:
    """Strict get — direct dict access, never None within contract."""
    profile = ConsumptionProfile.flat(value=0.42)
    assert profile.get(7, 0) == 0.42
    assert profile.get(12, 30) == 0.42
    assert CONSUMPTION_PER_30MIN == 0.45  # invariant guard


def test_consumption_profile_flat_overrides_value() -> None:
    """`flat(value=...)` populates every bucket with the provided value."""
    profile = ConsumptionProfile.flat(value=0.6)
    for h in range(7, 13):
        for m in (0, 30):
            assert profile.get(h, m) == 0.6
