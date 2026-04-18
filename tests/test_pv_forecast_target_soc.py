"""Tests for calculate_target_soc() with optional consumption_profile + trace."""

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
    result = calculate_target_soc(forecast)
    assert result.value == MIN_SOC_PERCENT
    assert len(result.buckets) == 12  # pełne okno 7-13 (12 × 30min)


def test_no_profile_matches_constant_consumption() -> None:
    """Backward compat: consumption_profile=None behaves exactly like before."""
    forecast = _make_forecast(0.3)  # PV 0.15 kWh/30min, deficit vs 0.45
    without = calculate_target_soc(forecast)
    with_none = calculate_target_soc(forecast, consumption_profile=None)
    assert without.value == with_none.value
    # With PV << consumption there's accumulated deficit → SOC > MIN
    assert without.value > MIN_SOC_PERCENT
    # Trace: consumption = const 0.45 for every bucket
    assert all(b.cons_kwh == CONSUMPTION_PER_30MIN for b in without.buckets)


def test_profile_with_higher_consumption_raises_soc() -> None:
    """Profile sugerujący wyższe ranne consumption → wyższy target SOC."""
    forecast = _make_forecast(0.6)  # PV 0.3 kWh/30min
    profile = ConsumptionProfile(
        buckets={
            (7, 0): 0.8,
            (7, 30): 0.8,
            (8, 0): 0.8,
            (8, 30): 0.8,
            (9, 0): 0.8,
            (9, 30): 0.5,
        }
    )
    baseline = calculate_target_soc(forecast)
    with_profile = calculate_target_soc(forecast, consumption_profile=profile)
    assert with_profile.value > baseline.value


def test_profile_with_lower_consumption_lowers_soc() -> None:
    """Profile sugerujący niższe consumption → niższy lub równy target SOC."""
    forecast = _make_forecast(0.6)
    profile = ConsumptionProfile(
        buckets={(h, m): 0.2 for h in range(7, 13) for m in (0, 30)}
    )
    baseline = calculate_target_soc(forecast)
    with_profile = calculate_target_soc(forecast, consumption_profile=profile)
    assert with_profile.value <= baseline.value


def test_partial_profile_falls_back_per_bucket() -> None:
    """Buckets z profile użyte, brakujące buckets → CONSUMPTION_PER_30MIN fallback."""
    forecast = _make_forecast(0.6)
    profile = ConsumptionProfile(buckets={(7, 0): 2.0})
    result = calculate_target_soc(forecast, consumption_profile=profile)
    baseline = calculate_target_soc(forecast)
    assert result.value >= baseline.value
    # Trace: first bucket = 2.0, rest = 0.45
    assert result.buckets[0].cons_kwh == 2.0
    assert result.buckets[1].cons_kwh == CONSUMPTION_PER_30MIN


def test_empty_profile_behaves_like_none() -> None:
    forecast = _make_forecast(0.3)
    empty = ConsumptionProfile(buckets={})
    assert (
        calculate_target_soc(forecast, consumption_profile=empty).value
        == calculate_target_soc(forecast).value
    )


def test_trace_has_is_min_flag() -> None:
    """Bucket z najbardziej ujemnym cumulative ma is_min=True (gdy jest deficit)."""
    forecast = _make_forecast(0.3)  # deficit przez całe okno
    result = calculate_target_soc(forecast)
    min_buckets = [b for b in result.buckets if b.is_min]
    assert len(min_buckets) == 1
    # Przy stałym deficycie — ostatni bucket ma największy (ujemny) cumulative
    assert min_buckets[0].period == "12:30"


def test_trace_contents() -> None:
    """Sprawdź że pojedynczy bucket trace ma oczekiwane pola."""
    forecast = _make_forecast(0.3)
    result = calculate_target_soc(forecast)
    b = result.buckets[0]
    assert b.period == "07:00"
    assert b.pv_kwh == 0.15
    assert b.cons_kwh == 0.45
    assert b.balance == round(0.15 - 0.45, 3)
    assert b.cumulative == b.balance
    b2 = result.buckets[1]
    assert b2.cumulative == round(2 * (0.15 - 0.45), 3)


def test_consumption_profile_get() -> None:
    p = ConsumptionProfile(buckets={(7, 0): 0.5, (7, 30): 0.6})
    assert p.get(7, 0) == 0.5
    assert p.get(7, 30) == 0.6
    assert p.get(8, 0) is None
    assert CONSUMPTION_PER_30MIN == 0.45  # invariant guard
