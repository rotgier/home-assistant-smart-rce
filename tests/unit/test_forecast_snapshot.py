"""ForecastSnapshot + ForecastContext archive form: round-trip and replay parity.

The archive is only worth keeping if a stored context, read back, makes every
production strategy compute exactly what was stored next to it. That is the
contract locked here; units in key names guard against the rate-vs-energy
mix-up (kWh/h summed as kWh) that once doubled a whole comparison.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from custom_components.smart_rce.domain.pv_forecast import (
    ForecastContext,
    LivePvSignals,
    PvForecast,
    PvForecasts,
    SolcastPeriod,
    WeatherConditionAtHour,
    WeatherConditions,
)
from custom_components.smart_rce.domain.pv_forecast.forecast_snapshot import (
    SCHEMA_VERSION,
    ForecastSnapshot,
    SnapshotTrigger,
)
import orjson
import pytest

_TZ = ZoneInfo("Europe/Warsaw")
_TODAY = date(2026, 10, 14)
_NOW = datetime(2026, 10, 14, 9, 11, 0, tzinfo=_TZ)


@pytest.fixture(autouse=True)
def _reset_bound_strategies():
    """Bound strategies are singletons — reset between tests."""
    _reset()
    yield
    _reset()


def _reset() -> None:
    for variant in PvForecast:
        variant.strategy.result = None
        variant.strategy.remaining_kwh = None


def _periods(day: date, rate: float) -> list[SolcastPeriod]:
    return [
        SolcastPeriod(
            period_start=f"{day.isoformat()}T{h:02d}:{m:02d}:00+02:00",
            pv_estimate=rate + h / 10,
            pv_estimate10=rate / 2,
            pv_estimate90=rate * 1.5 + 0.0123,
        )
        for h in range(6, 19)
        for m in (0, 30)
    ]


def _weather() -> WeatherConditions:
    tomorrow = date(2026, 10, 15)
    return WeatherConditions(
        conditions=[
            WeatherConditionAtHour(
                hour=h,
                condition_custom="sunny" if h < 11 else "partly_cloudy",
                forecast_date=_TODAY,
            )
            for h in range(6, 19)
        ]
        + [
            WeatherConditionAtHour(
                hour=h, condition_custom="cloudy", forecast_date=tomorrow
            )
            for h in range(6, 19)
        ]
        + [WeatherConditionAtHour(hour=12, condition_custom="rainy")]
    )


def _dispatched_forecasts() -> PvForecasts:
    """Run every trigger once so each variant has a result."""
    forecasts = PvForecasts()
    weather = _weather()
    forecasts.solcast_at_6_updated(_periods(_TODAY, 1.7), weather, _NOW)
    forecasts.solcast_today_updated(_periods(_TODAY, 2.1), weather, _NOW)
    forecasts.solcast_tomorrow_updated(_periods(date(2026, 10, 15), 1.3), weather, _NOW)
    forecasts.live_pv_updated(
        LivePvSignals(
            pv_power_w=2875.4,
            bucket_so_far_kwh=0.512,
            derivative_w_per_min=12.5,
            stability_stable=True,
        ),
        {(7, 0): 0.11, (7, 30): 0.3, (8, 0): 0.9, (8, 30): 1.234},
        consumption_w=640.0,
        start_charge_hour=None,
        now=_NOW,
    )
    return forecasts


def _capture(forecasts: PvForecasts) -> ForecastSnapshot:
    snapshot = ForecastSnapshot.capture(
        forecasts,
        taken_at=_NOW,
        trigger=SnapshotTrigger.SOLCAST,
        solcast_fetched_at="2026-10-14T07:10:37+00:00",
        code_version="abc123def456",
    )
    assert snapshot is not None
    return snapshot


def _through_json(data: dict) -> dict:
    """Do to a record what the archive file does: bytes and back."""
    return orjson.loads(orjson.dumps(data))


def test_capture_before_first_dispatch_is_none() -> None:
    snapshot = ForecastSnapshot.capture(
        PvForecasts(),
        taken_at=_NOW,
        trigger=SnapshotTrigger.GRID,
        solcast_fetched_at=None,
        code_version="x",
    )
    assert snapshot is None


def test_context_round_trip_is_exact() -> None:
    context = _dispatched_forecasts().last_context
    assert context is not None
    assert ForecastContext.from_dict(_through_json(context.to_dict())) == context


def test_snapshot_round_trip_is_exact() -> None:
    snapshot = _capture(_dispatched_forecasts())
    restored = ForecastSnapshot.from_dict(_through_json(snapshot.to_dict()))
    assert restored == snapshot


def test_snapshot_covers_every_variant_with_a_result() -> None:
    snapshot = _capture(_dispatched_forecasts())
    assert set(snapshot.variants) == {variant.key for variant in PvForecast}
    assert all(outcome.result is not None for outcome in snapshot.variants.values())


def test_replay_on_restored_context_reproduces_archived_results() -> None:
    """Parity: production strategies on the read-back context = stored outcomes."""
    archived = ForecastSnapshot.from_dict(
        _through_json(_capture(_dispatched_forecasts()).to_dict())
    )
    _reset()

    for variant in PvForecast:  # enum order = production dispatch order
        variant.strategy.update(archived.context)

    for variant in PvForecast:
        stored = archived.variants[variant.key]
        assert variant.result == stored.result, variant.key
        assert variant.strategy.remaining_kwh == stored.remaining_kwh, variant.key


def test_record_header_and_unit_named_keys() -> None:
    record = _capture(_dispatched_forecasts()).to_dict()

    assert record["schema_version"] == SCHEMA_VERSION
    assert record["kind"] == "forecast_snapshot"
    assert record["source"] == "live"
    assert record["trigger"] == "solcast"
    assert record["code_version"] == "abc123def456"
    context = record["context"]
    assert set(context["solcast_today"][0]) == {
        "start",
        "p50_kwh_h",
        "p10_kwh_h",
        "p90_kwh_h",
    }
    assert context["realized_pv_today_kwh"]["08:30"] == 1.234
    live = record["variants"][PvForecast.LIVE.key]["result"]
    assert set(live) == {"total_kwh", "periods"}
    assert set(live["periods"][0]) == {"start", "pv_kwh_h"}


def test_older_record_without_optional_keys_reads_with_defaults() -> None:
    """A field added later must not break reading records written before it."""
    context = ForecastContext.from_dict({"now": _NOW.isoformat()})
    assert context.signals == LivePvSignals()
    assert context.solcast_today == []
    assert context.realized_pv_today == {}
    assert context.consumption_w is None
