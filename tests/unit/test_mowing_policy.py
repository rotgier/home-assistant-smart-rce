"""Unit tests for the MowingPolicy aggregate (fresh-start threshold + persistence)."""

from custom_components.smart_rce.garden.domain.mowing_policy import MowingPolicy


def test_default_fresh_start_battery() -> None:
    assert MowingPolicy().fresh_start_battery == 90


def test_set_fresh_start_battery_changed_flag() -> None:
    policy = MowingPolicy()
    assert policy.set_fresh_start_battery(75) is True
    assert policy.fresh_start_battery == 75
    assert policy.set_fresh_start_battery(75) is False


def test_serialization_roundtrip() -> None:
    policy = MowingPolicy(fresh_start_battery=65)
    assert MowingPolicy.from_dict(policy.to_dict()).fresh_start_battery == 65


def test_from_dict_empty_defaults() -> None:
    assert MowingPolicy.from_dict({}).fresh_start_battery == 90
