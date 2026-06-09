"""Exhaustiveness test for `_matrix_key` in target_soc_matrix_service.

`_matrix_key` uses `match` + `assert_never` so a new `PvForecast` variant
without a `case` here raises `AssertionError` at import-time (module-level
dict comprehensions iterate every variant). This test catches the same
problem at pytest-time — CI/pre-commit picks it up before deploy, instead
of waiting for HA startup.

Plus regression-guard on key contract: today + tomorrow share key per
axis ('at_6' / 'live') because dashboards render only one resolver dict
per matrix view (no collision possible).
"""

from __future__ import annotations

from custom_components.smart_rce.application.target_soc_matrix_service import (
    _TODAY_PV_RESOLVERS,
    _TOMORROW_PV_RESOLVERS,
    _matrix_key,
)
from custom_components.smart_rce.domain.pv_forecast import PvForecast
import pytest


def test_matrix_key_covers_every_pv_forecast_variant() -> None:
    """Every enum member must have a `case` — else assert_never fires."""
    for variant in PvForecast:
        key = _matrix_key(variant)  # raises AssertionError if uncovered
        assert isinstance(key, str)
        assert key, f"empty matrix_key for {variant.name}"


def test_today_and_tomorrow_share_key_per_axis() -> None:
    """Today/tomorrow live in separate resolver dicts, so keys collide by design."""
    assert (
        _matrix_key(PvForecast.AT_6) == _matrix_key(PvForecast.TOMORROW_AT_6) == "at_6"
    )
    assert (
        _matrix_key(PvForecast.LIVE) == _matrix_key(PvForecast.TOMORROW_LIVE) == "live"
    )


def test_extrap_proportional_uses_abbreviated_cross_repo_key() -> None:
    """`extrap_propor` matches PV_LABELS lookup in target-soc-matrix-card.js."""
    assert _matrix_key(PvForecast.EXTRAP_PROPORTIONAL) == "extrap_propor"


def test_today_resolver_keys_match_dashboard_contract() -> None:
    """All 6 today variants present, keys match dashboard PV_LABELS dict."""
    assert set(_TODAY_PV_RESOLVERS.keys()) == {
        "at_6",
        "live",
        "extrap_pattern",
        "extrap_propor",
        "extrap_band",
        "extrap_band_recent",
    }


def test_tomorrow_resolver_keys_match_dashboard_contract() -> None:
    """Only 2 tomorrow variants (no extrap), share keys with today."""
    assert set(_TOMORROW_PV_RESOLVERS.keys()) == {"at_6", "live"}


def test_resolver_value_round_trip_matches_variant() -> None:
    """Resolver dict maps matrix_key → original variant (no scrambled refs)."""
    for v in PvForecast.today():
        assert _TODAY_PV_RESOLVERS[_matrix_key(v)] is v
    for v in PvForecast.tomorrow():
        assert _TOMORROW_PV_RESOLVERS[_matrix_key(v)] is v


# --- Negative case (regression guard) --- #


def test_matrix_key_raises_for_non_pv_forecast_input() -> None:
    """Safety net — assert_never path fires for non-enum inputs."""

    class Fake:
        name = "FAKE"

    with pytest.raises(AssertionError, match="unreachable"):
        _matrix_key(Fake())  # type: ignore[arg-type]
