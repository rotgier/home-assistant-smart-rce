"""Tests for weather_multiplier.compute_multiplier."""

from custom_components.smart_rce.domain.weather_multiplier import (
    MULTIPLIER_FLOOR,
    compute_multiplier,
)
import pytest


def test_no_rain_low_probability_returns_one():
    result = compute_multiplier(probability=10, amount_max_mm=2.0, duration_max_min=30)
    assert result.multiplier == 1.0
    assert result.penalty == 0.0


def test_no_rain_zero_duration_returns_one():
    result = compute_multiplier(probability=95, amount_max_mm=2.0, duration_max_min=0)
    assert result.multiplier == 1.0
    assert result.penalty == 0.0


def test_full_hour_heavy_storm_clamps_to_floor():
    # 60 min × heaviness=1 × confidence=1 → penalty = 1 * (0.4 + 0.5) * 1 = 0.9
    # multiplier = max(0.1, 1 - 0.9) = 0.1 (floor)
    result = compute_multiplier(
        probability=100, amount_max_mm=10.0, duration_max_min=60
    )
    assert result.multiplier == pytest.approx(0.1)
    assert result.coverage == 1.0
    assert result.heaviness == 1.0
    assert result.penalty == pytest.approx(0.9)


def test_brief_light_rain_partial_penalty():
    # 15 min × heaviness=0.1 (0.5/5) × confidence=1 (50/50)
    # penalty = 0.25 * (0.4 + 0.05) * 1 = 0.1125
    # multiplier ≈ 0.8875
    result = compute_multiplier(probability=50, amount_max_mm=0.5, duration_max_min=15)
    assert result.coverage == pytest.approx(0.25)
    assert result.heaviness == pytest.approx(0.1)
    assert result.penalty == pytest.approx(0.1125)
    assert result.multiplier == pytest.approx(0.8875)


def test_confidence_caps_at_one():
    # probability=80 → confidence=min(80/50, 1)=1.0 (same as prob=50)
    a = compute_multiplier(probability=50, amount_max_mm=1.0, duration_max_min=30)
    b = compute_multiplier(probability=80, amount_max_mm=1.0, duration_max_min=30)
    assert a.multiplier == pytest.approx(b.multiplier)


def test_heaviness_caps_at_one():
    # amount=5 vs amount=10 → both heaviness=1
    a = compute_multiplier(probability=80, amount_max_mm=5.0, duration_max_min=30)
    b = compute_multiplier(probability=80, amount_max_mm=10.0, duration_max_min=30)
    assert a.multiplier == pytest.approx(b.multiplier)


def test_none_inputs_treated_as_zero():
    # None duration → multiplier=1 (no-rain shortcut). amount=None too.
    result = compute_multiplier(
        probability=80, amount_max_mm=None, duration_max_min=None
    )
    assert result.multiplier == 1.0


def test_probability_threshold_boundary():
    # prob=29 (below 30) → no penalty
    below = compute_multiplier(probability=29, amount_max_mm=2.0, duration_max_min=60)
    assert below.multiplier == 1.0
    # prob=30 (at threshold) → penalty applies
    at_threshold = compute_multiplier(
        probability=30, amount_max_mm=2.0, duration_max_min=60
    )
    assert at_threshold.multiplier < 1.0


def test_multiplier_floor_never_zero():
    # Force a > 1.0 penalty to ensure floor clamp engages
    result = compute_multiplier(
        probability=100, amount_max_mm=100.0, duration_max_min=60
    )
    assert result.multiplier == MULTIPLIER_FLOOR
