"""Light coverage for EXTRAP `ForecastStrategy` subclasses.

Locks the contract for each of the 4 EXTRAP variants:
- `_compute` returns None when `PvForecast.LIVE.result` is None (chain
  dependency).
- `_compute` returns None when `ctx.solcast_today` is empty.
- After update with full inputs, `strategy.result` is populated +
  `remaining_kwh` is non-None.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.smart_rce.domain.pv_forecast import (
    ExtrapBandRecentStrategy,
    ExtrapBandStrategy,
    ExtrapPatternStrategy,
    ExtrapProportionalStrategy,
    ForecastContext,
    LivePvSignals,
    PvForecast,
    SolcastPeriod,
    WeatherConditionAtHour,
    WeatherConditions,
)
import pytest


@pytest.fixture(autouse=True)
def _reset_bound_strategies():
    """Bound strategies are singletons — reset between tests."""
    for variant in PvForecast:
        if variant.strategy is not None:
            variant.strategy.result = None
            variant.strategy.remaining_kwh = None
    yield
    for variant in PvForecast:
        if variant.strategy is not None:
            variant.strategy.result = None
            variant.strategy.remaining_kwh = None


def _solcast_today(target_date: str = "2026-01-15") -> list[SolcastPeriod]:
    """Full PV day Solcast periods (7:00-12:30)."""
    return [
        SolcastPeriod(
            period_start=f"{target_date}T{h:02d}:{m:02d}:00+01:00",
            pv_estimate=2.0,
            pv_estimate10=1.0,
            pv_estimate90=3.0,
        )
        for h in range(7, 13)
        for m in (0, 30)
    ]


def _weather() -> WeatherConditions:
    return WeatherConditions(
        conditions=[
            WeatherConditionAtHour(hour=h, condition_custom="sunny")
            for h in range(7, 13)
        ]
    )


def _populate_live() -> None:
    """Feed LIVE strategy so EXTRAP can read its result."""
    PvForecast.LIVE.strategy.update(
        ForecastContext(
            now=datetime(2026, 1, 15, 9, 0),
            signals=LivePvSignals(pv_power_w=1500.0, bucket_so_far_kwh=0.5),
            weather=_weather(),
            solcast_today=_solcast_today(),
        )
    )


def _ctx_with_live(now: datetime | None = None) -> ForecastContext:
    return ForecastContext(
        now=now or datetime(2026, 1, 15, 9, 0),
        signals=LivePvSignals(pv_power_w=1500.0, bucket_so_far_kwh=0.5),
        weather=_weather(),
        solcast_today=_solcast_today(),
        realized_pv_today={(7, 0): 0.5, (7, 30): 0.8, (8, 0): 1.0, (8, 30): 1.2},
        consumption_w=400.0,
        start_charge_hour=None,
    )


@pytest.mark.parametrize(
    "strategy_cls",
    [
        ExtrapPatternStrategy,
        ExtrapProportionalStrategy,
        ExtrapBandStrategy,
        ExtrapBandRecentStrategy,
    ],
)
def test_extrap_returns_none_when_live_result_missing(strategy_cls) -> None:
    """LIVE strategy unpopulated → EXTRAP returns None (chain dependency)."""
    # Don't call _populate_live() — LIVE.result stays None.
    strategy = strategy_cls()
    strategy.update(_ctx_with_live())
    assert strategy.result is None
    assert strategy.remaining_kwh is None


@pytest.mark.parametrize(
    "strategy_cls",
    [
        ExtrapPatternStrategy,
        ExtrapProportionalStrategy,
        ExtrapBandStrategy,
        ExtrapBandRecentStrategy,
    ],
)
def test_extrap_returns_none_when_solcast_today_empty(strategy_cls) -> None:
    """No solcast_today periods → EXTRAP returns None."""
    _populate_live()
    strategy = strategy_cls()
    ctx = ForecastContext(
        now=datetime(2026, 1, 15, 9, 0),
        signals=LivePvSignals(pv_power_w=1500.0, bucket_so_far_kwh=0.5),
        weather=_weather(),
        solcast_today=[],  # empty
        realized_pv_today={(7, 0): 0.5},
        consumption_w=400.0,
    )
    strategy.update(ctx)
    assert strategy.result is None


@pytest.mark.parametrize(
    "strategy_cls",
    [
        ExtrapPatternStrategy,
        ExtrapProportionalStrategy,
        ExtrapBandStrategy,
        ExtrapBandRecentStrategy,
    ],
)
def test_extrap_populates_result_and_remaining_kwh(strategy_cls) -> None:
    """Full inputs → strategy.result is PvForecastResult + remaining_kwh non-None."""
    _populate_live()
    strategy = strategy_cls()
    strategy.update(_ctx_with_live())
    assert strategy.result is not None
    assert strategy.remaining_kwh is not None
    assert strategy.remaining_kwh >= 0.0
    # total_kwh forwarded property
    assert strategy.total_kwh is not None


def test_all_extrap_variants_bound_on_enum() -> None:
    """Iter 3b: 4 EXTRAP variants have bound strategies."""
    assert isinstance(PvForecast.EXTRAP_PATTERN.strategy, ExtrapPatternStrategy)
    assert isinstance(
        PvForecast.EXTRAP_PROPORTIONAL.strategy, ExtrapProportionalStrategy
    )
    assert isinstance(PvForecast.EXTRAP_BAND.strategy, ExtrapBandStrategy)
    assert isinstance(PvForecast.EXTRAP_BAND_RECENT.strategy, ExtrapBandRecentStrategy)
