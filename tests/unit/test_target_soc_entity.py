"""Light coverage for `TargetSoc` per-variant domain entity.

Locks the contract:
- `recalculate(flat_cons, prev_cons, ctx)` populates flat + prev_days
- today-variants fail-hard when live signals missing
- tomorrow-variants ignore live signals
- `max` aggregates flat + prev_days, None when all None
- prev_days[i] = None when prev_cons[i] is None
- `is_today` forwarded from variant
"""

from __future__ import annotations

from datetime import date, datetime

from custom_components.smart_rce.domain.consumption_profiles import ConsumptionProfile
from custom_components.smart_rce.domain.pv_forecast import (
    ForecastContext,
    LivePvSignals,
    PvForecast,
    SolcastPeriod,
    WeatherConditionAtHour,
    WeatherConditions,
)
from custom_components.smart_rce.domain.target_soc import TargetSoc, TargetSocContext
import pytest


@pytest.fixture(autouse=True)
def _reset_bound_strategies():
    """Bound strategies are singletons — reset between tests."""
    for variant in PvForecast:
        if variant.strategy is not None:
            variant.strategy.result = None
    yield
    for variant in PvForecast:
        if variant.strategy is not None:
            variant.strategy.result = None


def _signals(pv_power_w: float | None = 1000.0) -> LivePvSignals:
    return LivePvSignals(pv_power_w=pv_power_w, bucket_so_far_kwh=0.1)


def _today_ctx(
    target_date: date = date(2026, 1, 15),
    live_consumption_w: float | None = 800.0,
) -> TargetSocContext:
    return TargetSocContext(
        target_date=target_date,
        signals=_signals(),
        live_consumption_w=live_consumption_w,
        start_charge_hour=None,
        now=datetime(2026, 1, 15, 9, 0),
    )


def _tomorrow_ctx() -> TargetSocContext:
    return TargetSocContext(
        target_date=date(2026, 1, 16),
        signals=_signals(),
        live_consumption_w=None,
        start_charge_hour=None,
        now=datetime(2026, 1, 15, 9, 0),
    )


def _populate_at_6_strategy() -> None:
    """Feed AT_6 bound strategy a context so its `result` is populated."""
    periods = [
        SolcastPeriod(
            period_start=f"2026-01-15T{h:02d}:{m:02d}:00+01:00",
            pv_estimate=2.0,
            pv_estimate10=1.0,
            pv_estimate90=3.0,
        )
        for h in range(7, 13)
        for m in (0, 30)
    ]
    weather = WeatherConditions(
        conditions=[
            WeatherConditionAtHour(hour=h, condition_custom="sunny")
            for h in range(7, 13)
        ]
    )
    ctx = ForecastContext(
        now=datetime(2026, 1, 15, 7, 0),
        signals=LivePvSignals(),
        weather=weather,
        solcast_at_6=periods,
    )
    PvForecast.AT_6.strategy.update(ctx)


def test_targetsoc_is_today_forwarded_from_variant() -> None:
    today_entity = TargetSoc(variant=PvForecast.AT_6)
    tomorrow_entity = TargetSoc(variant=PvForecast.TOMORROW_AT_6)
    assert today_entity.is_today is True
    assert tomorrow_entity.is_today is False


def test_recalculate_with_unbound_variant_keeps_flat_none() -> None:
    """Unbound variant (Iter 2: EXTRAP × 4) → variant.result is None → flat is None."""
    entity = TargetSoc(variant=PvForecast.EXTRAP_PATTERN)
    flat_cons = ConsumptionProfile.flat()
    entity.recalculate(flat_cons, [None] * 8, _today_ctx())
    assert entity.flat is None
    assert entity.prev_days == [None] * 8


def test_recalculate_today_fails_hard_when_live_consumption_w_missing() -> None:
    _populate_at_6_strategy()
    entity = TargetSoc(variant=PvForecast.AT_6)
    ctx = _today_ctx(live_consumption_w=None)
    entity.recalculate(ConsumptionProfile.flat(), [None] * 8, ctx)
    assert entity.flat is None  # fail-hard: today needs live signals


def test_recalculate_today_with_live_signals_populates_flat() -> None:
    _populate_at_6_strategy()
    entity = TargetSoc(variant=PvForecast.AT_6)
    entity.recalculate(ConsumptionProfile.flat(), [None] * 8, _today_ctx())
    assert entity.flat is not None
    assert entity.flat.value >= 10  # MIN_SOC_PERCENT or higher


def test_recalculate_tomorrow_ignores_live_consumption() -> None:
    # TOMORROW_AT_6 unbound in Iter 2 — flat stays None due to no forecast result.
    # This test verifies the is_today=False branch path works without live signals.
    entity = TargetSoc(variant=PvForecast.TOMORROW_AT_6)
    ctx = _tomorrow_ctx()
    # No exception even though live_consumption_w is None
    entity.recalculate(ConsumptionProfile.flat(), [None] * 8, ctx)
    assert entity.flat is None  # because variant.result is None (unbound)


def test_max_none_when_all_results_none() -> None:
    entity = TargetSoc(variant=PvForecast.AT_6)
    assert entity.max is None


def test_max_aggregates_flat_and_prev_days() -> None:
    _populate_at_6_strategy()
    entity = TargetSoc(variant=PvForecast.AT_6)
    entity.recalculate(ConsumptionProfile.flat(), [None] * 8, _today_ctx())
    # With AT_6 bound and live signals → flat is set; max should equal flat.value
    assert entity.max == entity.flat.value


def test_prev_days_length_matches_input_length() -> None:
    entity = TargetSoc(variant=PvForecast.AT_6)
    # Empty prev_cons → prev_days becomes empty
    entity.recalculate(ConsumptionProfile.flat(), [], _today_ctx())
    assert entity.prev_days == []
    # 3-element prev_cons → prev_days has 3 None entries
    entity.recalculate(ConsumptionProfile.flat(), [None] * 3, _today_ctx())
    assert entity.prev_days == [None, None, None]
