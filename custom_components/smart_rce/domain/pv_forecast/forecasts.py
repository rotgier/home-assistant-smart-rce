"""PvForecasts — orchestrator caching inputs + dispatching to strategies.

DDD split from `TargetSocCatalog`: this aggregate owns the "what PV looks
like" concern (8 forecast scenarios + PV-side live signals), while
`TargetSocCatalog` shrinks to the "what battery target SOC results from
forecast + consumption" concern.

Public API is **trigger-source-named**: each method takes only the delta
that changed (Solcast at_6 / live / tomorrow periods, weather, live PV
signals + per-tick cons knobs for EXTRAP strategies). The forecasts
caches all inputs and rebuilds the full `ForecastContext` per dispatch.

After Iter 3b: every PvForecast variant has a bound `ForecastStrategy`
(AT_6 + LIVE + TOMORROW × 2 + EXTRAP × 4). No legacy dicts; `get(variant)`
is just `variant.result`. `remaining_kwh(variant)` exposes the strategy
side of the unifying contract. `live_pv_updated` carries realized_pv +
cons knobs needed by EXTRAP — service pushes them per tick.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .forecast_enum import PvForecast
from .strategy_base import (
    ForecastContext,
    LivePvSignals,
    PvForecastResult,
    SolcastPeriod,
    WeatherConditions,
)

__all__ = [
    "LivePvSignals",
    "PvForecast",
    "PvForecasts",
]


@dataclass
class PvForecasts:
    """Orchestrates `PvForecast` strategy updates + caches inputs.

    Callers push only what changed via trigger-named methods; the forecasts
    builds the full `ForecastContext` from cached inputs and dispatches
    to all bound strategies.
    """

    # — Cached inputs (rebuilt into ForecastContext per dispatch) —
    _signals: LivePvSignals = field(default_factory=LivePvSignals)
    _weather: WeatherConditions = field(default_factory=WeatherConditions.empty)
    _solcast_at_6: list[SolcastPeriod] = field(default_factory=list)
    _solcast_today: list[SolcastPeriod] = field(default_factory=list)
    _solcast_tomorrow: list[SolcastPeriod] = field(default_factory=list)
    # EXTRAP strategy inputs (carried in ctx). Realized PV history from
    # recorder; cons knobs forwarded from TargetSocCatalog.inputs.
    _realized_pv_today: dict[tuple[int, int], float] = field(default_factory=dict)
    _consumption_w: float | None = None
    _start_charge_hour: int | None = None

    # ─── Read API ──────────────────────────────────────────────────────────

    def get(self, variant: PvForecast) -> PvForecastResult | None:
        """Return forecast result for `variant` — straight from its bound strategy."""
        return variant.result

    def remaining_kwh(self, variant: PvForecast) -> float | None:
        """Strategy-bound remaining kWh from now onwards (None when no result)."""
        if variant.strategy is None:
            return None
        return variant.strategy.remaining_kwh

    def all(self) -> dict[PvForecast, PvForecastResult | None]:
        """Snapshot dict of every variant → forecast (or None)."""
        return {variant: variant.result for variant in PvForecast}

    def today(self) -> dict[PvForecast, PvForecastResult | None]:
        """Snapshot of today-axis variants (AT_6, LIVE, 4× EXTRAP)."""
        return {v: v.result for v in PvForecast.today()}

    def tomorrow(self) -> dict[PvForecast, PvForecastResult | None]:
        """Snapshot of tomorrow-axis variants (TOMORROW_AT_6, TOMORROW_LIVE)."""
        return {v: v.result for v in PvForecast.tomorrow()}

    @property
    def signals(self) -> LivePvSignals:
        """Read-only snapshot of current PV-side live readings."""
        return self._signals

    @property
    def solcast_today(self) -> list[SolcastPeriod]:
        """Raw Solcast live periods — exposed for downstream consumers."""
        return self._solcast_today

    # ─── Trigger-named public API: each takes only the delta ────────────────

    def solcast_at_6_updated(
        self,
        periods: list[SolcastPeriod],
        weather: WeatherConditions,
        now: datetime,
    ) -> None:
        """Solcast at_6 entity changed (~once daily 06:00)."""
        self._solcast_at_6 = periods
        self._weather = weather
        self._dispatch(now)

    def solcast_today_updated(
        self,
        periods: list[SolcastPeriod],
        weather: WeatherConditions,
        now: datetime,
    ) -> None:
        """Solcast live entity changed (continuous updates).

        LIVE strategy picks up via dispatch; EXTRAP strategies also
        dispatch on the same tick (they depend on LIVE.result).
        """
        self._solcast_today = periods
        self._weather = weather
        self._dispatch(now)

    def solcast_tomorrow_updated(
        self,
        periods: list[SolcastPeriod],
        weather: WeatherConditions,
        now: datetime,
    ) -> None:
        """Solcast tomorrow entity changed."""
        self._solcast_tomorrow = periods
        self._weather = weather
        self._dispatch(now)

    def weather_updated(
        self,
        weather: WeatherConditions,
        now: datetime,
    ) -> None:
        """Weather forecast changed — re-dispatch every variant."""
        self._weather = weather
        self._dispatch(now)

    def live_pv_updated(
        self,
        signals: LivePvSignals,
        realized_pv_today: dict[tuple[int, int], float],
        consumption_w: float | None,
        start_charge_hour: int | None,
        now: datetime,
    ) -> None:
        """Per-minute tick — PV signals + realized PV + cons knobs refreshed.

        Bound today strategies re-patch in-progress bucket; EXTRAP
        strategies recompute on signal/realized-PV change. Service
        pushes all per-tick inputs in a single call.
        """
        self._signals = signals
        self._realized_pv_today = realized_pv_today
        self._consumption_w = consumption_w
        self._start_charge_hour = start_charge_hour
        self._dispatch(now)

    # ─── Internal ──────────────────────────────────────────────────────────

    def _dispatch(self, now: datetime) -> None:
        """Build ctx from cached inputs + dispatch to bound strategies."""
        ctx = ForecastContext(
            now=now,
            signals=self._signals,
            weather=self._weather,
            solcast_at_6=self._solcast_at_6,
            solcast_today=self._solcast_today,
            solcast_tomorrow=self._solcast_tomorrow,
            realized_pv_today=self._realized_pv_today,
            consumption_w=self._consumption_w,
            start_charge_hour=self._start_charge_hour,
        )
        for variant in PvForecast:
            if variant.strategy is not None:
                variant.strategy.update(ctx)
