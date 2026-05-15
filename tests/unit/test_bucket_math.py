"""Tests for `bucket_math` — Bucket VO + bucket arithmetic."""

from __future__ import annotations

from datetime import datetime, timezone
import math

from custom_components.smart_rce.domain.bucket_math import Bucket, buckets_from_now
import pytest

_TZ = timezone.utc


def test_bucket_minute_must_be_0_or_30() -> None:
    Bucket(9, 0)
    Bucket(9, 30)
    with pytest.raises(ValueError, match="Bucket minute must be 0 or 30"):
        Bucket(9, 15)


def test_enclosing_at_minute_below_30() -> None:
    now = datetime(2026, 5, 15, 9, 13, tzinfo=_TZ)
    assert Bucket.enclosing(now) == Bucket(9, 0)


def test_enclosing_at_minute_30_or_above() -> None:
    now = datetime(2026, 5, 15, 9, 45, tzinfo=_TZ)
    assert Bucket.enclosing(now) == Bucket(9, 30)


def test_enclosing_at_bucket_boundary() -> None:
    # 09:00:00 — exactly on boundary, belongs to 09:00 bucket
    assert Bucket.enclosing(datetime(2026, 5, 15, 9, 0, tzinfo=_TZ)) == Bucket(9, 0)
    # 09:30:00 — belongs to 09:30 bucket
    assert Bucket.enclosing(datetime(2026, 5, 15, 9, 30, tzinfo=_TZ)) == Bucket(9, 30)


def test_remaining_sec_at_inside_bucket() -> None:
    bucket = Bucket(9, 0)
    now = datetime(2026, 5, 15, 9, 13, tzinfo=_TZ)
    assert bucket.remaining_sec_at(now) == (30 - 13) * 60  # 1020 sec


def test_remaining_sec_at_microsecond_precision() -> None:
    bucket = Bucket(9, 0)
    now = datetime(2026, 5, 15, 9, 13, 42, 500_000, tzinfo=_TZ)
    # 17 min - 42.5 sec
    expected = 17 * 60 - 42.5
    assert math.isclose(bucket.remaining_sec_at(now), expected, rel_tol=1e-9)


def test_is_in_progress_at() -> None:
    bucket = Bucket(9, 0)
    # before bucket
    assert not bucket.is_in_progress_at(datetime(2026, 5, 15, 8, 59, tzinfo=_TZ))
    # at start
    assert bucket.is_in_progress_at(datetime(2026, 5, 15, 9, 0, tzinfo=_TZ))
    # inside
    assert bucket.is_in_progress_at(datetime(2026, 5, 15, 9, 15, tzinfo=_TZ))
    # at end (exclusive)
    assert not bucket.is_in_progress_at(datetime(2026, 5, 15, 9, 30, tzinfo=_TZ))
    # past end
    assert not bucket.is_in_progress_at(datetime(2026, 5, 15, 9, 45, tzinfo=_TZ))


def test_is_closed_at() -> None:
    bucket = Bucket(9, 0)
    assert not bucket.is_closed_at(datetime(2026, 5, 15, 9, 15, tzinfo=_TZ))
    # at end -> closed
    assert bucket.is_closed_at(datetime(2026, 5, 15, 9, 30, tzinfo=_TZ))
    assert bucket.is_closed_at(datetime(2026, 5, 15, 10, 0, tzinfo=_TZ))


def test_is_future_at() -> None:
    bucket = Bucket(10, 0)
    assert bucket.is_future_at(datetime(2026, 5, 15, 9, 13, tzinfo=_TZ))
    assert not bucket.is_future_at(datetime(2026, 5, 15, 10, 0, tzinfo=_TZ))
    assert not bucket.is_future_at(datetime(2026, 5, 15, 10, 15, tzinfo=_TZ))


def test_bucket_live_remaining_kwh_at_constant_power() -> None:
    now = datetime(2026, 5, 15, 9, 13, tzinfo=_TZ)
    # 1500 W × 1020s / 3600s/h / 1000W/kW = 0.425 kWh
    expected = 1.5 * 1020 / 3600
    assert math.isclose(Bucket.live_remaining_kwh(now, 1500.0), expected, rel_tol=1e-9)


def test_bucket_live_remaining_kwh_symmetric_for_pv_and_cons() -> None:
    """Formula is identical regardless of power source — same W → same kWh."""
    now = datetime(2026, 5, 15, 9, 13, tzinfo=_TZ)
    assert Bucket.live_remaining_kwh(now, 800.0) == Bucket.live_remaining_kwh(
        now, 800.0
    )


def test_bucket_full_bucket_kwh_combines_so_far_and_extrap() -> None:
    now = datetime(2026, 5, 15, 9, 13, tzinfo=_TZ)
    so_far = 0.4
    pv_w = 1500.0
    expected = so_far + Bucket.live_remaining_kwh(now, pv_w)
    assert math.isclose(
        Bucket.full_bucket_kwh(now, pv_w, so_far), expected, rel_tol=1e-9
    )


def test_buckets_from_now_classifies_closed_in_progress_future() -> None:
    buckets = {(h, m): 1.0 for h in range(7, 13) for m in (0, 30)}
    now = datetime(2026, 5, 15, 9, 13, tzinfo=_TZ)
    view = buckets_from_now(buckets, now=now, live_remaining_kwh=0.7)
    # closed: (7,0)..(8,30)
    for h, m in [(7, 0), (7, 30), (8, 0), (8, 30)]:
        assert view[(h, m)] == 0.0
    # in-progress: (9, 0)
    assert view[(9, 0)] == 0.7
    # future: (9, 30)..(12, 30)
    for (h, m), v in view.items():
        if (h, m) > (9, 0):
            assert v == 1.0
