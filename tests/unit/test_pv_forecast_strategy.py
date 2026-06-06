"""Light coverage for `ForecastStrategy` hierarchy + `PvForecast` enum binding.

Pure-function adjust algorithms (`_adjust_pv_forecast_at6/live`) are
exercised end-to-end via existing target_soc + integration tests; here
we lock the strategy template-method contract:

- `_compute` returns fresh adjusted from full ctx, None when input missing
- `update` caches non-None results
- `update` re-patches in-progress bucket using live signals on today-variants
- `PvForecast.AT_6.result` reads `strategy.result` (no shim for unbound)
"""

from __future__ import annotations

from datetime import datetime

from custom_components.smart_rce.domain.pv_forecast import (
    LivePvSignals,
    SolcastPeriod,
    WeatherConditionAtHour,
)
from custom_components.smart_rce.domain.pv_forecast_strategy import (
    At6Strategy,
    ForecastContext,
    ForecastStrategy,
    LiveStrategy,
    PvForecast,
)
import pytest


@pytest.fixture(autouse=True)
def _reset_bound_strategies():
    """Strategies are singletons bound to enum members — reset between tests."""
    for variant in PvForecast:
        if variant.strategy is not None:
            variant.strategy.result = None
    yield
    for variant in PvForecast:
        if variant.strategy is not None:
            variant.strategy.result = None


def _solcast_periods(target_date: str = "2026-01-15") -> list[SolcastPeriod]:
    """Two morning periods at 7:00 + 7:30 with simple est rates."""
    return [
        SolcastPeriod(
            period_start=f"{target_date}T07:00:00+01:00",
            pv_estimate=1.0,
            pv_estimate10=0.5,
            pv_estimate90=1.5,
        ),
        SolcastPeriod(
            period_start=f"{target_date}T07:30:00+01:00",
            pv_estimate=2.0,
            pv_estimate10=1.0,
            pv_estimate90=3.0,
        ),
    ]


def _weather() -> list[WeatherConditionAtHour]:
    """Sunny at hour 7 (matches the test periods)."""
    return [
        WeatherConditionAtHour(hour=7, condition_custom="sunny"),
    ]


def test_at6_strategy_returns_none_when_inputs_missing() -> None:
    strategy = At6Strategy()
    ctx = ForecastContext(now=datetime(2026, 1, 15, 7, 0), signals=LivePvSignals())
    strategy.update(ctx)
    assert strategy.result is None


def test_at6_strategy_caches_compute_result() -> None:
    strategy = At6Strategy()
    ctx = ForecastContext(
        now=datetime(2026, 1, 15, 7, 0),
        signals=LivePvSignals(),
        weather=_weather(),
        solcast_at_6=_solcast_periods(),
    )
    strategy.update(ctx)
    assert strategy.result is not None
    assert len(strategy.result.forecast) == 2


def test_at6_strategy_preserves_cached_when_compute_returns_none() -> None:
    """Second update with no Solcast keeps the previously cached result."""
    strategy = At6Strategy()
    ctx_full = ForecastContext(
        now=datetime(2026, 1, 15, 7, 0),
        signals=LivePvSignals(),
        weather=_weather(),
        solcast_at_6=_solcast_periods(),
    )
    strategy.update(ctx_full)
    cached = strategy.result

    ctx_empty = ForecastContext(
        now=datetime(2026, 1, 15, 7, 5), signals=LivePvSignals()
    )
    strategy.update(ctx_empty)
    assert strategy.result is cached  # preserved, not recomputed


def test_at6_strategy_tomorrow_reads_solcast_tomorrow() -> None:
    """At6Strategy(today=False) reads ctx.solcast_tomorrow, not ctx.solcast_at_6."""
    strategy = At6Strategy(today=False)
    ctx = ForecastContext(
        now=datetime(2026, 1, 15, 7, 0),
        signals=LivePvSignals(),
        weather=_weather(),
        solcast_at_6=_solcast_periods(),  # today snapshot — should be IGNORED
        solcast_tomorrow=[],  # tomorrow empty → result None
    )
    strategy.update(ctx)
    assert strategy.result is None

    # Now provide tomorrow periods — strategy picks them up.
    ctx_tomorrow = ForecastContext(
        now=datetime(2026, 1, 15, 7, 0),
        signals=LivePvSignals(),
        weather=_weather(),
        solcast_tomorrow=_solcast_periods(),
    )
    strategy.update(ctx_tomorrow)
    assert strategy.result is not None
    assert len(strategy.result.forecast) == 2


def test_live_strategy_tomorrow_reads_solcast_tomorrow() -> None:
    """LiveStrategy(today=False) reads ctx.solcast_tomorrow, not ctx.solcast_today."""
    strategy = LiveStrategy(today=False)
    ctx = ForecastContext(
        now=datetime(2026, 1, 15, 7, 0),
        signals=LivePvSignals(),
        weather=_weather(),
        solcast_today=_solcast_periods(),  # today live — should be IGNORED
    )
    strategy.update(ctx)
    assert strategy.result is None

    ctx_tomorrow = ForecastContext(
        now=datetime(2026, 1, 15, 7, 0),
        signals=LivePvSignals(),
        weather=_weather(),
        solcast_tomorrow=_solcast_periods(),
    )
    strategy.update(ctx_tomorrow)
    assert strategy.result is not None


def test_tomorrow_strategies_skip_in_progress_patch() -> None:
    """today=False strategies skip the in-progress chart patch (no matching bucket)."""
    today_strategy = At6Strategy(today=True)
    tomorrow_strategy = At6Strategy(today=False)
    assert today_strategy.supports_in_progress_patch is True
    assert tomorrow_strategy.supports_in_progress_patch is False


def test_live_strategy_caches_compute_result() -> None:
    strategy = LiveStrategy()
    ctx = ForecastContext(
        now=datetime(2026, 1, 15, 7, 0),
        signals=LivePvSignals(),
        weather=_weather(),
        solcast_today=_solcast_periods(),
    )
    strategy.update(ctx)
    assert strategy.result is not None


def test_pv_forecast_at_6_property_reads_strategy_result() -> None:
    """PvForecast.AT_6.result is a thin pass-through to its bound strategy."""
    # Reset strategy result (singletons persist across tests in this module).
    PvForecast.AT_6.strategy.result = None
    assert PvForecast.AT_6.result is None

    ctx = ForecastContext(
        now=datetime(2026, 1, 15, 7, 0),
        signals=LivePvSignals(),
        weather=_weather(),
        solcast_at_6=_solcast_periods(),
    )
    PvForecast.AT_6.strategy.update(ctx)
    assert PvForecast.AT_6.result is PvForecast.AT_6.strategy.result
    assert PvForecast.AT_6.result is not None


def test_all_variants_have_bound_strategy() -> None:
    """Iter 3b: every PvForecast variant has a bound ForecastStrategy."""
    for variant in PvForecast:
        assert variant.strategy is not None, f"{variant} should be bound"
    # Empty result before any update — all return None gracefully.
    for variant in PvForecast:
        # Reset strategy.result to None (fixture should already do this)
        assert variant.result is None


def test_forecast_strategy_base_raises_not_implemented() -> None:
    """Concrete subclass must override _compute."""
    base = ForecastStrategy()
    ctx = ForecastContext(now=datetime(2026, 1, 15, 7, 0), signals=LivePvSignals())
    try:
        base.update(ctx)
    except NotImplementedError:
        return
    raise AssertionError("ForecastStrategy._compute should raise NotImplementedError")
