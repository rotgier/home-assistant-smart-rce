"""Tests for `PvForecastResult.to_profile()` now-aware time-shift.

Symmetric to `test_to_view_time_shift.py` but for PV side. Integrates
`pv_power_w_5min` over remaining seconds in the in-progress bucket
internally — caller passes raw W, not pre-computed kWh.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import math

from custom_components.smart_rce.domain.pv_forecast import (
    AdjustedPeriod,
    PvForecastResult,
)
import pytest

_TZ = timezone.utc


def _forecast(
    rate_kwh_per_h: float = 2.0, target_date: date = date(2026, 5, 14)
) -> PvForecastResult:
    """Constant-rate forecast 7:00-13:00 on `target_date` (rate kWh/h per period)."""
    periods = [
        AdjustedPeriod(
            period_start=f"{target_date.isoformat()}T{h:02d}:{mm:02d}:00+00:00",
            pv_estimate_adjusted=rate_kwh_per_h,
        )
        for h in range(7, 13)
        for mm in (0, 30)
    ]
    total_kwh = (rate_kwh_per_h / 2) * len(periods)
    return PvForecastResult(forecast=periods, total_kwh=total_kwh)


def test_now_none_returns_forecast_snapshot() -> None:
    """now=None → full forecast snapshot (back-compat)."""
    f = _forecast(rate_kwh_per_h=2.0)
    profile = f.to_profile(date(2026, 5, 14))
    # 2 kWh/h → 1 kWh per 30-min bucket
    for h in range(7, 13):
        for m in (0, 30):
            assert math.isclose(profile.get(h, m), 1.0, rel_tol=1e-9)


def test_now_without_pv_power_raises() -> None:
    """Fail-hard contract: pv_power_w_5min required when `now` is given."""
    f = _forecast()
    with pytest.raises(ValueError, match="pv_power_w_5min"):
        f.to_profile(date(2026, 5, 14), now=datetime(2026, 5, 14, 9, 13, tzinfo=_TZ))


def test_now_before_window_all_future_full() -> None:
    """now=06:00 → every bucket future, full forecast kWh (no time-shift)."""
    f = _forecast(rate_kwh_per_h=2.0)
    profile = f.to_profile(
        date(2026, 5, 14),
        now=datetime(2026, 5, 14, 6, 0, tzinfo=_TZ),
        pv_power_w_5min=0.0,  # not used (no in-progress bucket)
    )
    for h in range(7, 13):
        for m in (0, 30):
            assert math.isclose(profile.get(h, m), 1.0, rel_tol=1e-9)


def test_now_after_window_all_closed() -> None:
    """now=13:30 → every bucket closed → 0.0."""
    f = _forecast(rate_kwh_per_h=2.0)
    profile = f.to_profile(
        date(2026, 5, 14),
        now=datetime(2026, 5, 14, 13, 30, tzinfo=_TZ),
        pv_power_w_5min=0.0,
    )
    for h in range(7, 13):
        for m in (0, 30):
            assert profile.get(h, m) == 0.0


def test_now_inside_bucket_overrides_with_live_pv() -> None:
    """now=09:13 → 09:00 bucket = pv_power_w_5min x 17min/60 / 1000."""
    f = _forecast(rate_kwh_per_h=2.0)  # forecast = 1 kWh per bucket
    now = datetime(2026, 5, 14, 9, 13, tzinfo=_TZ)
    pv_w = 1500.0  # 1.5 kW current
    profile = f.to_profile(date(2026, 5, 14), now=now, pv_power_w_5min=pv_w)
    remaining_sec = (30 - 13) * 60
    expected = (pv_w / 1000.0) * remaining_sec / 3600.0
    assert math.isclose(profile.get(9, 0), expected, rel_tol=1e-9)
    # Past closed
    assert profile.get(8, 30) == 0.0
    # Future unchanged
    assert math.isclose(profile.get(9, 30), 1.0, rel_tol=1e-9)


def test_pv_power_zero_kills_in_progress_bucket() -> None:
    """pv_power_w_5min=0 → in-progress bucket = 0 (sun went behind cloud)."""
    f = _forecast(rate_kwh_per_h=2.0)
    profile = f.to_profile(
        date(2026, 5, 14),
        now=datetime(2026, 5, 14, 9, 13, tzinfo=_TZ),
        pv_power_w_5min=0.0,
    )
    assert profile.get(9, 0) == 0.0
    # Future still has forecast
    assert math.isclose(profile.get(10, 0), 1.0, rel_tol=1e-9)


def test_now_at_bucket_boundary_full_bucket() -> None:
    """now=09:00 → 09:00 bucket = pv_power_w_5min * 1800s/3600 = pv_w/2."""
    f = _forecast(rate_kwh_per_h=2.0)
    pv_w = 2000.0
    profile = f.to_profile(
        date(2026, 5, 14),
        now=datetime(2026, 5, 14, 9, 0, tzinfo=_TZ),
        pv_power_w_5min=pv_w,
    )
    expected = (pv_w / 1000.0) * 1800.0 / 3600.0  # = 1.0 kWh
    assert math.isclose(profile.get(9, 0), expected, rel_tol=1e-9)
