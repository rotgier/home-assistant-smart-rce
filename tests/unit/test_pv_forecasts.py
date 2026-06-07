"""Light coverage for `PvForecasts` read API + signals.

Update method behavior (catalog.update_from_solcast_*, tick_minute) is
exercised end-to-end via the existing target_soc + sensor tests + live
deploy; here we lock the read API contract so consumers (sensors, matrix
service, future ChargePlanner) can rely on it.
"""

from __future__ import annotations

from custom_components.smart_rce.domain.pv_forecast import (
    LivePvSignals,
    PvForecast,
    PvForecasts,
)
import pytest


@pytest.fixture(autouse=True)
def _reset_bound_strategies():
    """Strategies are singletons bound to enum members — reset between tests."""
    for variant in PvForecast:
        if variant.strategy is not None:
            variant.strategy.result = None
            variant.strategy.remaining_kwh = None
    yield
    for variant in PvForecast:
        if variant.strategy is not None:
            variant.strategy.result = None
            variant.strategy.remaining_kwh = None


def test_strategy_enum_coverage() -> None:
    """All 8 strategies partition cleanly into today (6) and tomorrow (2)."""
    assert len(list(PvForecast)) == 8
    assert len(PvForecast.today()) == 6
    assert len(PvForecast.tomorrow()) == 2
    assert len(PvForecast.extrap()) == 4
    all_listed = set(PvForecast.today()) | set(PvForecast.tomorrow())
    assert all_listed == set(PvForecast)


def test_empty_catalog_read_api_returns_none() -> None:
    """Fresh catalog: every strategy returns None; signals VO has all-None fields."""
    catalog = PvForecasts()
    for strategy in PvForecast:
        assert catalog.get(strategy) is None
        assert catalog.remaining_kwh(strategy) is None
    assert catalog.signals == LivePvSignals()
    assert catalog.solcast_today == []


def test_today_and_tomorrow_views_have_expected_keys() -> None:
    """today() / tomorrow() expose subset dicts keyed by their respective strategies."""
    catalog = PvForecasts()
    assert set(catalog.today().keys()) == set(PvForecast.today())
    assert set(catalog.tomorrow().keys()) == set(PvForecast.tomorrow())
    assert set(catalog.all().keys()) == set(PvForecast)


def test_live_pv_updated_replaces_signals_atomically() -> None:
    """live_pv_updated replaces all 4 signal fields in one call (Tell-Don't-Ask)."""
    from datetime import datetime

    forecasts = PvForecasts()
    forecasts.live_pv_updated(
        LivePvSignals(
            pv_power_w=1500.0,
            bucket_so_far_kwh=0.3,
            derivative_w_per_min=60.0,
            stability_stable=True,
        ),
        realized_pv_today={},
        consumption_w=None,
        start_charge_hour=None,
        now=datetime(2026, 1, 1, 12, 0),
    )
    snap = forecasts.signals
    assert snap.pv_power_w == 1500.0
    assert snap.bucket_so_far_kwh == 0.3
    assert snap.derivative_w_per_min == 60.0
    assert snap.stability_stable is True

    # Second tick fully replaces — no field-by-field merge.
    forecasts.live_pv_updated(
        LivePvSignals(pv_power_w=2000.0),
        realized_pv_today={},
        consumption_w=None,
        start_charge_hour=None,
        now=datetime(2026, 1, 1, 12, 1),
    )
    snap = forecasts.signals
    assert snap.pv_power_w == 2000.0
    assert snap.bucket_so_far_kwh is None
    assert snap.derivative_w_per_min is None
    assert snap.stability_stable is None
