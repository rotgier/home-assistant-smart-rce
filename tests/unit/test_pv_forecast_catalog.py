"""Light coverage for `PvForecastCatalog` read API + signals.

Update method behavior (catalog.update_from_solcast_*, tick_minute) is
exercised end-to-end via the existing target_soc + sensor tests + live
deploy; here we lock the read API contract so consumers (sensors, matrix
service, future ChargePlanner) can rely on it.
"""

from __future__ import annotations

from custom_components.smart_rce.domain.pv_forecast_catalog import (
    EXTRAP_STRATEGIES,
    TODAY_STRATEGIES,
    TOMORROW_STRATEGIES,
    LivePvSignals,
    PvForecastCatalog,
    PvStrategy,
)


def test_strategy_enum_coverage() -> None:
    """All 8 strategies partition cleanly into today (6) and tomorrow (2)."""
    assert len(list(PvStrategy)) == 8
    assert len(TODAY_STRATEGIES) == 6
    assert len(TOMORROW_STRATEGIES) == 2
    assert len(EXTRAP_STRATEGIES) == 4
    all_listed = set(TODAY_STRATEGIES) | set(TOMORROW_STRATEGIES)
    assert all_listed == set(PvStrategy)


def test_empty_catalog_read_api_returns_none() -> None:
    """Fresh catalog: every strategy returns None; signals VO has all-None fields."""
    catalog = PvForecastCatalog()
    for strategy in PvStrategy:
        assert catalog.get(strategy) is None
    for strategy in EXTRAP_STRATEGIES:
        bundle = catalog.get_extrapolated(strategy)
        assert bundle is not None  # initialized to ExtrapolatedLive.empty()
        assert bundle.adjusted is None
        assert bundle.remaining_kwh is None
        assert bundle.target_soc is None
    assert catalog.signals == LivePvSignals()
    assert catalog.solcast_live == []


def test_today_and_tomorrow_views_have_expected_keys() -> None:
    """today() / tomorrow() expose subset dicts keyed by their respective strategies."""
    catalog = PvForecastCatalog()
    assert set(catalog.today().keys()) == set(TODAY_STRATEGIES)
    assert set(catalog.tomorrow().keys()) == set(TOMORROW_STRATEGIES)
    assert set(catalog.all().keys()) == set(PvStrategy)


def test_refresh_live_signals_replaces_atomically() -> None:
    """refresh_live_signals replaces all 4 fields in one call (Tell-Don't-Ask)."""
    catalog = PvForecastCatalog()
    catalog.refresh_live_signals(
        LivePvSignals(
            pv_power_w=1500.0,
            bucket_so_far_kwh=0.3,
            derivative_w_per_min=60.0,
            stability_stable=True,
        )
    )
    snap = catalog.signals
    assert snap.pv_power_w == 1500.0
    assert snap.bucket_so_far_kwh == 0.3
    assert snap.derivative_w_per_min == 60.0
    assert snap.stability_stable is True

    # Second refresh fully replaces — no field-by-field merge.
    catalog.refresh_live_signals(LivePvSignals(pv_power_w=2000.0))
    snap = catalog.signals
    assert snap.pv_power_w == 2000.0
    assert snap.bucket_so_far_kwh is None
    assert snap.derivative_w_per_min is None
    assert snap.stability_stable is None
