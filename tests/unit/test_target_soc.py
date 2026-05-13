"""Tests for `calculate_target_soc()` with symmetric PvProfile + ConsumptionProfile.

Function lives in `domain/target_soc.py`. Inputs are strict 12-bucket
value objects (`PvProfile` + `ConsumptionProfile`) covering 7:00..12:30.
Callers either build profiles from forecasts via
`AdjustedPvForecast.to_profile(target_date)` or pass synthetic baselines
via `PvProfile.flat()` / `ConsumptionProfile.flat()`.
"""

from __future__ import annotations

from datetime import date

from custom_components.smart_rce.domain.pv_forecast import (
    AdjustedPeriod,
    AdjustedPvForecast,
    ConsumptionProfile,
)
from custom_components.smart_rce.domain.target_soc import (
    CONSUMPTION_PER_30MIN,
    MIN_SOC_PERCENT,
    PvProfile,
    calculate_target_soc as _calculate_target_soc,
)
import pytest


def _make_profile(rate_kwh_per_h: float) -> PvProfile:
    """Constant-rate PvProfile 7:00..12:30 (rate_kwh_per_h / 2 per bucket)."""
    return PvProfile.flat(value=rate_kwh_per_h / 2)


def _make_forecast(
    rate_kwh_per_h: float, target_date: str = "2026-04-18"
) -> AdjustedPvForecast:
    """Constant-rate forecast 7:00-13:00 (12 periods of 30min) on `target_date`."""
    y, m, d = (int(p) for p in target_date.split("-"))
    periods = [
        AdjustedPeriod(
            period_start=f"{y:04d}-{m:02d}-{d:02d}T{h:02d}:{mm:02d}:00+02:00",
            pv_estimate_adjusted=rate_kwh_per_h,
        )
        for h in range(7, 13)
        for mm in (0, 30)
    ]
    total_kwh = (rate_kwh_per_h / 2) * len(periods)
    return AdjustedPvForecast(forecast=periods, total_kwh=total_kwh)


def test_surplus_pv_returns_min_soc() -> None:
    """Gdy PV pokrywa consumption, no deficit → MIN_SOC."""
    result = _calculate_target_soc(_make_profile(2.0), ConsumptionProfile.flat())
    assert result.value == MIN_SOC_PERCENT
    assert len(result.buckets) == 12


def test_flat_profile_matches_constant_consumption() -> None:
    """Flat profile reproduces the constant CONSUMPTION_PER_30MIN baseline."""
    result = _calculate_target_soc(_make_profile(0.3), ConsumptionProfile.flat())
    assert result.value > MIN_SOC_PERCENT
    assert all(b.cons_kwh == CONSUMPTION_PER_30MIN for b in result.buckets)


def test_profile_with_higher_consumption_raises_soc() -> None:
    """Profile sugerujący wyższe ranne consumption → wyższy target SOC."""
    pv = _make_profile(0.6)
    buckets = {(h, m): CONSUMPTION_PER_30MIN for h in range(7, 13) for m in (0, 30)}
    for slot in [(7, 0), (7, 30), (8, 0), (8, 30), (9, 0)]:
        buckets[slot] = 0.8
    buckets[(9, 30)] = 0.5
    profile = ConsumptionProfile(buckets=buckets)
    baseline = _calculate_target_soc(pv, ConsumptionProfile.flat())
    with_profile = _calculate_target_soc(pv, profile)
    assert with_profile.value > baseline.value


def test_profile_with_lower_consumption_lowers_soc() -> None:
    """Profile sugerujący niższe consumption → niższy lub równy target SOC."""
    pv = _make_profile(0.6)
    profile = ConsumptionProfile(
        buckets={(h, m): 0.2 for h in range(7, 13) for m in (0, 30)}
    )
    baseline = _calculate_target_soc(pv, ConsumptionProfile.flat())
    with_profile = _calculate_target_soc(pv, profile)
    assert with_profile.value <= baseline.value


def test_partial_cons_profile_raises_validation_error() -> None:
    with pytest.raises(ValueError, match="missing="):
        ConsumptionProfile(buckets={(7, 0): 2.0})


def test_empty_cons_profile_raises_validation_error() -> None:
    with pytest.raises(ValueError, match="missing="):
        ConsumptionProfile(buckets={})


def test_extra_cons_buckets_raise_validation_error() -> None:
    full = {(h, m): CONSUMPTION_PER_30MIN for h in range(7, 13) for m in (0, 30)}
    full[(13, 0)] = 0.5
    with pytest.raises(ValueError, match="extra="):
        ConsumptionProfile(buckets=full)


def test_partial_pv_profile_raises_validation_error() -> None:
    with pytest.raises(ValueError, match="missing="):
        PvProfile(buckets={(7, 0): 0.5})


def test_extra_pv_buckets_raise_validation_error() -> None:
    full = {(h, m): 0.5 for h in range(7, 13) for m in (0, 30)}
    full[(13, 0)] = 1.0
    with pytest.raises(ValueError, match="extra="):
        PvProfile(buckets=full)


def test_trace_has_is_min_flag() -> None:
    result = _calculate_target_soc(_make_profile(0.3), ConsumptionProfile.flat())
    min_buckets = [b for b in result.buckets if b.is_min]
    assert len(min_buckets) == 1
    assert min_buckets[0].period == "12:30"


def test_trace_contents() -> None:
    result = _calculate_target_soc(_make_profile(0.3), ConsumptionProfile.flat())
    b = result.buckets[0]
    assert b.period == "07:00"
    assert b.pv_kwh == 0.15
    assert b.cons_kwh == 0.45
    assert b.balance == round(0.15 - 0.45, 3)
    assert b.cumulative == b.balance
    b2 = result.buckets[1]
    assert b2.cumulative == round(2 * (0.15 - 0.45), 3)


def test_consumption_profile_get_returns_float() -> None:
    profile = ConsumptionProfile.flat(value=0.42)
    assert profile.get(7, 0) == 0.42
    assert profile.get(12, 30) == 0.42
    assert CONSUMPTION_PER_30MIN == 0.45


def test_consumption_profile_flat_overrides_value() -> None:
    profile = ConsumptionProfile.flat(value=0.6)
    for h in range(7, 13):
        for m in (0, 30):
            assert profile.get(h, m) == 0.6


def test_pv_profile_flat_defaults_to_zero() -> None:
    profile = PvProfile.flat()
    assert all(profile.get(h, m) == 0.0 for h in range(7, 13) for m in (0, 30))


def test_to_profile_single_date_inferred() -> None:
    """Without `target_date`, picks the date of the first period."""
    profile = _make_forecast(1.0).to_profile()
    # 1.0 kWh/h × 0.5 = 0.5 kWh per 30-min bucket
    assert all(profile.get(h, m) == 0.5 for h in range(7, 13) for m in (0, 30))


def test_to_profile_explicit_target_date_filters() -> None:
    """Periods for another day are ignored; missing buckets → 0.0."""
    profile = _make_forecast(1.0, target_date="2026-04-18").to_profile(
        target_date=date(2026, 4, 18)
    )
    assert profile.get(7, 0) == 0.5


def test_to_profile_no_match_raises() -> None:
    """target_date outside forecast coverage → ValueError."""
    forecast = _make_forecast(1.0, target_date="2026-04-18")
    with pytest.raises(ValueError, match="no periods match"):
        forecast.to_profile(target_date=date(2026, 4, 20))


def test_in_progress_bucket_internally_time_prorated() -> None:
    """At now=09:13, bucket 09:00 yields full × (17/30); future buckets full."""
    from datetime import datetime, timezone

    pv = _make_profile(2.0)  # 1.0 kWh per 30-min bucket
    cons = ConsumptionProfile.flat()  # 0.45 per bucket
    now = datetime(2026, 4, 18, 9, 13, tzinfo=timezone.utc)
    result = _calculate_target_soc(pv, cons, now=now)
    first = result.buckets[0]
    assert first.period == "09:00"
    # remaining = 17min → factor = 17/30 ≈ 0.567
    factor = 17 / 30
    assert abs(first.pv_kwh - round(1.0 * factor, 3)) < 0.002
    assert abs(first.cons_kwh - round(0.45 * factor, 3)) < 0.002
    # Bucket past in-progress keeps full values.
    second = result.buckets[1]
    assert second.period == "09:30"
    assert second.pv_kwh == 1.0
    assert second.cons_kwh == 0.45


def test_live_consumption_w_overrides_cons_for_in_progress_bucket() -> None:
    """`live_consumption_w` injects current-power cons remaining; profile ignored for that bucket."""
    from datetime import datetime, timezone

    pv = _make_profile(2.0)  # 1.0 kWh per 30-min
    cons = ConsumptionProfile.flat(value=0.45)
    now = datetime(2026, 4, 18, 9, 13, tzinfo=timezone.utc)
    # 1200 W × (17min / 60min/h) = 0.34 kWh remaining
    result = _calculate_target_soc(pv, cons, now=now, live_consumption_w=1200.0)
    first = result.buckets[0]
    expected = round(1.2 * (17 / 60), 3)
    assert abs(first.cons_kwh - expected) < 0.002
    # Non-current bucket still uses profile.
    assert result.buckets[1].cons_kwh == 0.45


def test_live_consumption_w_no_effect_outside_window() -> None:
    """When `now` is before 7:00, no in-progress bucket — full-window simulation."""
    from datetime import datetime, timezone

    pv = _make_profile(2.0)
    cons = ConsumptionProfile.flat()
    now = datetime(2026, 4, 18, 5, 0, tzinfo=timezone.utc)
    result = _calculate_target_soc(pv, cons, now=now, live_consumption_w=1200.0)
    # First bucket is 07:00 — not "current", full values.
    assert result.buckets[0].period == "07:00"
    assert result.buckets[0].pv_kwh == 1.0
    assert result.buckets[0].cons_kwh == 0.45


def test_to_profile_missing_buckets_filled_with_zero() -> None:
    """When forecast covers only part of 7-13, missing buckets default to 0.0."""
    periods = [
        AdjustedPeriod(
            period_start="2026-04-18T08:00:00+02:00", pv_estimate_adjusted=1.0
        ),
        AdjustedPeriod(
            period_start="2026-04-18T08:30:00+02:00", pv_estimate_adjusted=1.0
        ),
    ]
    forecast = AdjustedPvForecast(forecast=periods, total_kwh=1.0)
    profile = forecast.to_profile()
    assert profile.get(7, 0) == 0.0
    assert profile.get(8, 0) == 0.5
    assert profile.get(12, 30) == 0.0
