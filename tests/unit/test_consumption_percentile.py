"""Consumption percentile — linear interpolation, rank k = (n - 1) * fraction."""

from custom_components.smart_rce.domain.consumption_percentile import (
    CONSUMPTION_PERCENTILE,
    percentile,
    percentile_by_key,
)
import pytest


def test_eight_days_interpolate_between_fifth_and_sixth():
    # k = 7 * 0.6 = 4.2 -> 5th value + 0.2 of the step to the 6th.
    values = [0.8, 0.1, 0.7, 0.2, 0.6, 0.3, 0.5, 0.4]
    assert percentile(values, CONSUMPTION_PERCENTILE) == pytest.approx(0.52)


def test_median_is_the_half_percentile():
    assert percentile([3.0, 1.0, 2.0, 10.0], 0.5) == pytest.approx(2.5)


def test_single_value_is_itself():
    assert percentile([0.42], 0.6) == 0.42


def test_two_values():
    assert percentile([1.0, 2.0], 0.6) == pytest.approx(1.6)


def test_no_values_raise():
    with pytest.raises(ValueError, match="no values"):
        percentile([], 0.6)


def test_per_key_counts_only_profiles_that_have_the_key():
    profiles = [{9: 1.0, 10: 0.2}, {9: 3.0}, {9: 2.0, 10: 0.4}]
    assert percentile_by_key(profiles) == pytest.approx({9: 2.2, 10: 0.32})
