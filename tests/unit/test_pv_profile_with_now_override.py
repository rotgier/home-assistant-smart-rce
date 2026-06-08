"""Tests for `PvProfile.with_now_override()` time-shift behavior.

Symmetric to `ConsumptionProfile.to_view()` — closed buckets zero out,
in-progress bucket keeps only the live-power × remaining-sec fraction,
future buckets pass through unchanged.

Plus `PvProfile.from_realized_buckets()` factory — builds the 12-bucket
profile from a `RealizedPvLoader`-shaped dict.
"""

from __future__ import annotations

from datetime import datetime, timezone

from custom_components.smart_rce.domain.bucket import Bucket, Buckets
from custom_components.smart_rce.domain.target_soc import PvProfile
import pytest

_TZ = timezone.utc


def _profile_with_distinct_values() -> PvProfile:
    """Distinct value per bucket so positional shifts are visible."""
    return PvProfile(
        buckets=Buckets(
            by_bucket={
                Bucket(h, m): 0.10 + 0.01 * ((h - 7) * 2 + (1 if m == 30 else 0))
                for h in range(7, 13)
                for m in (0, 30)
            }
        )
    )


# --- from_realized_buckets --- #


def test_from_realized_buckets_empty_dict_fills_all_zero() -> None:
    profile = PvProfile.from_realized_buckets({})
    assert all(profile.get(h, m) == 0.0 for h in range(7, 13) for m in (0, 30))


def test_from_realized_buckets_sparse_values_only_at_given_slots() -> None:
    realized = {(8, 30): 1.5, (9, 0): 2.0}
    profile = PvProfile.from_realized_buckets(realized)
    assert profile.get(7, 0) == 0.0
    assert profile.get(8, 30) == 1.5
    assert profile.get(9, 0) == 2.0
    assert profile.get(12, 30) == 0.0


def test_from_realized_buckets_full_window_preserves_values() -> None:
    realized = {(h, m): float(h * 10 + m) for h in range(7, 13) for m in (0, 30)}
    profile = PvProfile.from_realized_buckets(realized)
    for h in range(7, 13):
        for m in (0, 30):
            assert profile.get(h, m) == float(h * 10 + m)


# --- with_now_override --- #


def test_now_none_returns_self() -> None:
    p = _profile_with_distinct_values()
    assert p.with_now_override(now=None) is p


def test_now_without_pv_power_w_raises() -> None:
    """Fail-hard contract: pv_power_w_5min required when `now` is given."""
    p = _profile_with_distinct_values()
    with pytest.raises(ValueError, match="pv_power_w_5min"):
        p.with_now_override(now=datetime(2026, 5, 14, 9, 13, tzinfo=_TZ))


def test_with_now_override_zeros_past_keeps_future() -> None:
    """At now=09:15, buckets ending <= 09:15 are 0; future buckets unchanged."""
    p = _profile_with_distinct_values()
    now = datetime(2026, 5, 14, 9, 15, tzinfo=_TZ)
    # pv_power_w=2000 → in-progress 09:00 bucket = 2000 × 900s / 3_600_000 = 0.5 kWh
    view = p.with_now_override(now=now, pv_power_w_5min=2000.0)
    # Past: bucket [7:00, 7:30) ends 7:30 ≤ 9:15 → 0
    assert view.get(7, 0) == 0.0
    assert view.get(7, 30) == 0.0
    assert view.get(8, 0) == 0.0
    assert view.get(8, 30) == 0.0
    # In-progress: bucket [9:00, 9:30) contains 9:15, remaining 15 min
    assert view.get(9, 0) == pytest.approx(0.5, abs=1e-6)
    # Future: passed through unchanged from source profile
    assert view.get(9, 30) == p.get(9, 30)
    assert view.get(10, 0) == p.get(10, 0)
    assert view.get(12, 30) == p.get(12, 30)


def test_with_now_override_zero_power_makes_in_progress_zero() -> None:
    """Zero pv_power_w_5min → in-progress bucket contributes 0 kWh."""
    p = _profile_with_distinct_values()
    now = datetime(2026, 5, 14, 9, 15, tzinfo=_TZ)
    view = p.with_now_override(now=now, pv_power_w_5min=0.0)
    assert view.get(9, 0) == 0.0
    # Future still unchanged
    assert view.get(9, 30) == p.get(9, 30)


def test_with_now_override_idempotent_when_called_twice_with_same_args() -> None:
    """Applying with_now_override twice with same now is a no-op on result."""
    p = _profile_with_distinct_values()
    now = datetime(2026, 5, 14, 10, 0, tzinfo=_TZ)
    once = p.with_now_override(now=now, pv_power_w_5min=1000.0)
    twice = once.with_now_override(now=now, pv_power_w_5min=1000.0)
    for h in range(7, 13):
        for m in (0, 30):
            assert once.get(h, m) == twice.get(h, m)
