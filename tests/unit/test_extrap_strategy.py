"""Light coverage for EXTRAP `ForecastStrategy` subclasses.

Locks the contract for each of the 6 EXTRAP variants:
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
    ExtrapBlendStrategy,
    ExtrapPatternStrategy,
    ExtrapProportionalCappedStrategy,
    ExtrapProportionalStrategy,
    ForecastContext,
    LivePvSignals,
    PvForecast,
    PvForecastResult,
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
        ExtrapProportionalCappedStrategy,
        ExtrapBlendStrategy,
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
        ExtrapProportionalCappedStrategy,
        ExtrapBlendStrategy,
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
        ExtrapProportionalCappedStrategy,
        ExtrapBlendStrategy,
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
    assert isinstance(
        PvForecast.EXTRAP_PROPORTIONAL_CAPPED.strategy,
        ExtrapProportionalCappedStrategy,
    )
    assert isinstance(PvForecast.EXTRAP_BLEND.strategy, ExtrapBlendStrategy)


# --- Capped / Blend: bounds and fade ---

_SP = SolcastPeriod(
    period_start="2026-01-15T10:00:00+01:00",
    pv_estimate=2.0,
    pv_estimate10=1.0,
    pv_estimate90=4.0,
)


@pytest.mark.parametrize(
    ("rate", "expected"),
    [(0.4, 1.0), (1.5, 1.5), (2.5, 2.5), (5.0, 2.6)],  # cap = 2 + 0.3 x 2
)
def test_capped_keeps_the_rate_between_p10_and_a_third_toward_p90(rate, expected):
    assert ExtrapProportionalCappedStrategy._bound(rate, _SP) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("hours_ahead", "weight"),
    [(0.25, 1.0), (1.0, 1.0), (3.5, 0.5), (6.0, 0.25)],
)
def test_blend_trusts_the_extrapolation_an_hour_then_fades(hours_ahead, weight):
    assert ExtrapBlendStrategy._weight(hours_ahead) == pytest.approx(weight)


def test_blend_sits_between_capped_and_live_far_ahead() -> None:
    """Realized well above median → capped above Live; blend in between later."""
    _populate_live()
    ctx = ForecastContext(
        now=datetime(2026, 1, 15, 8, 0),
        signals=LivePvSignals(pv_power_w=2500.0, bucket_so_far_kwh=0.1),
        weather=_weather(),
        solcast_today=_solcast_today(),
        realized_pv_today={(7, 0): 1.4, (7, 30): 1.5},
        consumption_w=400.0,
    )
    capped, blend = ExtrapProportionalCappedStrategy(), ExtrapBlendStrategy()
    capped.update(ctx)
    blend.update(ctx)
    live = PvForecast.LIVE.result
    assert capped.result is not None and blend.result is not None and live
    rate = {
        name: {str(p.period_start)[11:16]: p.pv_estimate_adjusted for p in res.forecast}
        for name, res in (("c", capped.result), ("b", blend.result), ("l", live))
    }
    assert rate["b"]["08:30"] == pytest.approx(rate["c"]["08:30"])  # first hour
    far = "12:30"
    assert (
        min(rate["l"][far], rate["c"][far])
        < rate["b"][far]
        < max(rate["l"][far], rate["c"][far])
    )


@pytest.mark.parametrize(
    ("now", "realized", "buckets"),
    [
        # Dawn: only the bucket just started has a score.
        (datetime(2026, 1, 15, 7, 5), {}, 1),
        (datetime(2026, 1, 15, 7, 35), {(7, 0): 0.4}, 2),
        # A past bucket without a realized value does not count.
        (datetime(2026, 1, 15, 8, 5), {(7, 30): 0.6}, 2),
        (datetime(2026, 1, 15, 9, 0), {(7, 0): 0.5, (7, 30): 0.8, (8, 30): 1.2}, 4),
    ],
)
@pytest.mark.parametrize(
    "strategy_cls",
    [ExtrapProportionalStrategy, ExtrapProportionalCappedStrategy, ExtrapBlendStrategy],
)
def test_result_counts_the_buckets_its_score_rests_on(
    strategy_cls, now, realized, buckets
) -> None:
    """`scored_buckets` = scored buckets, the current one included (17.10.2026)."""
    _populate_live()
    strategy = strategy_cls()
    strategy.update(
        ForecastContext(
            now=now,
            signals=LivePvSignals(pv_power_w=1500.0, bucket_so_far_kwh=0.1),
            weather=_weather(),
            solcast_today=_solcast_today(),
            realized_pv_today=realized,
            consumption_w=400.0,
        )
    )
    assert strategy.result is not None
    assert strategy.result.scored_buckets == buckets
    assert strategy.result.can_cap_margin() is (buckets >= 2)


def test_bucket_count_survives_the_archive_and_live_has_none() -> None:
    _populate_live()
    strategy = ExtrapBlendStrategy()
    strategy.update(_ctx_with_live())
    result, live = strategy.result, PvForecast.LIVE.result
    assert result is not None
    assert live is not None
    assert PvForecastResult.from_dict(result.to_dict()) == result
    # Live never extrapolates: no count, not archived, always caps.
    assert live.scored_buckets is None
    assert "scored_buckets" not in live.to_dict()
    assert live.can_cap_margin()
    # Archived before the counter existed: reads as no count.
    old = {k: v for k, v in result.to_dict().items() if k != "scored_buckets"}
    assert PvForecastResult.from_dict(old).scored_buckets is None
