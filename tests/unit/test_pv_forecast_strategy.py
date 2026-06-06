"""Light coverage for `ForecastStrategy` hierarchy + `PvForecast` enum binding.

Pure-function adjust algorithms (`_adjust_pv_forecast_at6/live`) are
exercised end-to-end via existing target_soc + integration tests; here
we lock the strategy template-method contract:

- `_compute` returns fresh adjusted from full ctx, None when input missing
- `update` caches non-None results
- `update` re-patches in-progress bucket using live signals on today-variants
- `PvForecast.AT_6.adjusted` reads `strategy.adjusted` (no shim for unbound)
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
    assert strategy.adjusted is None


def test_at6_strategy_caches_compute_result() -> None:
    strategy = At6Strategy()
    ctx = ForecastContext(
        now=datetime(2026, 1, 15, 7, 0),
        signals=LivePvSignals(),
        weather=_weather(),
        solcast_at_6=_solcast_periods(),
    )
    strategy.update(ctx)
    assert strategy.adjusted is not None
    assert len(strategy.adjusted.forecast) == 2


def test_at6_strategy_preserves_cached_when_compute_returns_none() -> None:
    """Second update with no Solcast keeps the previously cached adjusted."""
    strategy = At6Strategy()
    ctx_full = ForecastContext(
        now=datetime(2026, 1, 15, 7, 0),
        signals=LivePvSignals(),
        weather=_weather(),
        solcast_at_6=_solcast_periods(),
    )
    strategy.update(ctx_full)
    cached = strategy.adjusted

    ctx_empty = ForecastContext(
        now=datetime(2026, 1, 15, 7, 5), signals=LivePvSignals()
    )
    strategy.update(ctx_empty)
    assert strategy.adjusted is cached  # preserved, not recomputed


def test_live_strategy_caches_compute_result() -> None:
    strategy = LiveStrategy()
    ctx = ForecastContext(
        now=datetime(2026, 1, 15, 7, 0),
        signals=LivePvSignals(),
        weather=_weather(),
        solcast_live=_solcast_periods(),
    )
    strategy.update(ctx)
    assert strategy.adjusted is not None


def test_pv_forecast_at_6_property_reads_strategy_adjusted() -> None:
    """PvForecast.AT_6.adjusted is a thin pass-through to its bound strategy."""
    # Reset strategy adjusted (singletons persist across tests in this module).
    PvForecast.AT_6.strategy.adjusted = None
    assert PvForecast.AT_6.adjusted is None

    ctx = ForecastContext(
        now=datetime(2026, 1, 15, 7, 0),
        signals=LivePvSignals(),
        weather=_weather(),
        solcast_at_6=_solcast_periods(),
    )
    PvForecast.AT_6.strategy.update(ctx)
    assert PvForecast.AT_6.adjusted is PvForecast.AT_6.strategy.adjusted
    assert PvForecast.AT_6.adjusted is not None


def test_unbound_variants_have_no_strategy() -> None:
    """Iter 1b: TOMORROW_* and EXTRAP_* are unbound (strategy=None)."""
    assert PvForecast.AT_6.strategy is not None
    assert PvForecast.LIVE.strategy is not None
    assert PvForecast.TOMORROW_AT_6.strategy is None
    assert PvForecast.TOMORROW_LIVE.strategy is None
    assert PvForecast.EXTRAP_PATTERN.strategy is None
    assert PvForecast.EXTRAP_PROPORTIONAL.strategy is None
    assert PvForecast.EXTRAP_BAND.strategy is None
    assert PvForecast.EXTRAP_BAND_RECENT.strategy is None
    # Their `adjusted` property gracefully returns None.
    assert PvForecast.TOMORROW_AT_6.adjusted is None
    assert PvForecast.EXTRAP_PATTERN.adjusted is None


def test_forecast_strategy_base_raises_not_implemented() -> None:
    """Concrete subclass must override _compute."""
    base = ForecastStrategy()
    ctx = ForecastContext(now=datetime(2026, 1, 15, 7, 0), signals=LivePvSignals())
    try:
        base.update(ctx)
    except NotImplementedError:
        return
    raise AssertionError("ForecastStrategy._compute should raise NotImplementedError")
