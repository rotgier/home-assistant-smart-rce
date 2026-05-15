"""Tests for `ConsumptionProfile.to_view()` time-shift behavior.

Method moves the in-progress bucket logic from `calculate_target_soc` into
the profile constructor — closed buckets zero out, in-progress bucket
keeps only the remaining fraction (either time-prorated forecast or
`live_consumption_w` override), future buckets pass through unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math

from custom_components.smart_rce.domain.bucket import Bucket, Buckets
from custom_components.smart_rce.domain.consumption_profiles import ConsumptionProfile
import pytest

_TZ = timezone.utc


def _profile() -> ConsumptionProfile:
    """Distinct value per bucket so we can tell positional shifts apart."""
    return ConsumptionProfile(
        buckets=Buckets(
            by_bucket={
                Bucket(h, m): 0.10 + 0.01 * ((h - 7) * 2 + (1 if m == 30 else 0))
                for h in range(7, 13)
                for m in (0, 30)
            }
        )
    )


def test_now_none_returns_self() -> None:
    p = _profile()
    assert p.to_view(now=None) is p


def test_now_without_live_consumption_w_raises() -> None:
    """Fail-hard contract: live_consumption_w required when `now` is given."""
    p = _profile()
    with pytest.raises(ValueError, match="live_consumption_w"):
        p.to_view(now=datetime(2026, 5, 14, 9, 13, tzinfo=_TZ))


def test_now_before_window_all_future() -> None:
    """Now=06:00 → every 7:00..12:30 bucket is full forecast (no time-shift)."""
    p = _profile()
    view = p.to_view(
        now=datetime(2026, 5, 14, 6, 0, tzinfo=_TZ), live_consumption_w=0.0
    )
    for bucket, full in p.buckets.items():
        assert view.get(bucket.hour, bucket.minute) == full


def test_now_after_window_all_closed() -> None:
    """Now=13:30 → every bucket closed → 0.0."""
    p = _profile()
    view = p.to_view(
        now=datetime(2026, 5, 14, 13, 30, tzinfo=_TZ), live_consumption_w=0.0
    )
    for bucket in p.buckets:
        assert view.get(bucket.hour, bucket.minute) == 0.0


def test_now_at_bucket_boundary_uses_live_for_full_bucket() -> None:
    """Now=09:00 exactly → 09:00 bucket = live_w x 1800s/3600 = live_w/2 kWh."""
    p = _profile()
    live_w = 1200.0
    view = p.to_view(
        now=datetime(2026, 5, 14, 9, 0, tzinfo=_TZ), live_consumption_w=live_w
    )
    # Past closed
    for bucket in p.buckets:
        if (bucket.hour, bucket.minute) < (9, 0):
            assert view.get(bucket.hour, bucket.minute) == 0.0
    # 09:00 bucket: live override over full 1800s
    expected = (live_w / 1000.0) * 1800.0 / 3600.0  # = 0.6 kWh
    assert math.isclose(view.get(9, 0), expected, rel_tol=1e-9)
    # Future unchanged
    assert view.get(9, 30) == p.get(9, 30)


def test_now_inside_bucket_uses_live() -> None:
    """Now=09:13 in [09:00..09:30): 09:00 bucket = live_w * 17min/60, past 0, future unchanged."""
    p = _profile()
    now = datetime(2026, 5, 14, 9, 13, tzinfo=_TZ)
    live_w = 1200.0
    view = p.to_view(now=now, live_consumption_w=live_w)
    remaining_sec = (30 - 13) * 60  # 1020s
    expected = (live_w / 1000.0) * remaining_sec / 3600.0
    assert math.isclose(view.get(9, 0), expected, rel_tol=1e-9)
    # Past
    assert view.get(8, 30) == 0.0
    # Future
    assert view.get(9, 30) == p.get(9, 30)
    assert view.get(12, 30) == p.get(12, 30)


def test_live_consumption_w_zero_means_zero() -> None:
    """live_consumption_w=0 → in-progress bucket = 0 (no consumption now)."""
    p = _profile()
    now = datetime(2026, 5, 14, 9, 13, tzinfo=_TZ)
    view = p.to_view(now=now, live_consumption_w=0.0)
    assert view.get(9, 0) == 0.0


def test_source_date_preserved() -> None:
    from datetime import date

    p = ConsumptionProfile.flat(value=0.1, source_date=date(2026, 5, 7))
    view = p.to_view(
        now=datetime(2026, 5, 14, 9, 0, tzinfo=_TZ), live_consumption_w=0.0
    )
    assert view.source_date == date(2026, 5, 7)


def test_live_consumption_w_only_affects_in_progress() -> None:
    """Override touches only the in-progress bucket; future buckets stay forecast."""
    p = ConsumptionProfile.flat(value=0.5)
    now = datetime(2026, 5, 14, 9, 15, tzinfo=_TZ)
    view = p.to_view(now=now, live_consumption_w=10_000.0)  # extreme value
    # Future bucket — forecast, untouched
    assert view.get(10, 0) == 0.5
    assert view.get(12, 30) == 0.5


def test_view_returns_new_instance_when_time_shifted() -> None:
    """to_view with now != None returns a fresh ConsumptionProfile (frozen-safe)."""
    p = _profile()
    view = p.to_view(
        now=datetime(2026, 5, 14, 9, 0, tzinfo=_TZ), live_consumption_w=0.0
    )
    assert view is not p
    # buckets dict should be a new mapping too
    assert view.buckets is not p.buckets


def test_flat_profile_works_with_to_view() -> None:
    """Smoke: flat baseline + to_view doesn't violate 12-bucket contract."""
    p = ConsumptionProfile.flat()
    view = p.to_view(
        now=datetime(2026, 5, 14, 9, 15, tzinfo=_TZ), live_consumption_w=900.0
    )
    assert len(list(view.buckets.values())) == 12
    # 09:00 bucket: 900W x 900s / 3600 = 0.225 kWh
    expected = 0.9 * 900.0 / 3600.0
    assert math.isclose(view.get(9, 0), expected, rel_tol=1e-9)
