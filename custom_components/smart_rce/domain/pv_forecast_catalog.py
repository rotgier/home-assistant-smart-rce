"""PvForecastUpdater — orchestrator caching inputs + dispatching to strategies.

DDD split from `TargetSocCatalog`: this aggregate owns the "what PV looks
like" concern (8 forecast scenarios + extrapolation + PV-side live
signals), while `TargetSocCatalog` shrinks to the "what battery target
SoC results from forecast + consumption" concern.

Public API is **trigger-source-named**: each method takes only the delta
that changed (Solcast at_6 periods, Solcast live periods, weather, live
PV signals). The updater caches all inputs and rebuilds the full
`ForecastContext` per dispatch.

Iter 1b mid-state: AT_6 + LIVE bound to `ForecastStrategy` instances
(results in `PvForecast.X.strategy.adjusted`). The remaining 6 variants
(TOMORROW × 2 + EXTRAP × 4) still flow through the legacy `_forecasts`
/ `_extrapolated` dicts + module-level adjust helpers. Iter 3 binds the
rest and Iter 4 drops the transitional dicts +
`refresh_extrap_inputs`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import pv_forecast_extrapolation
from .pv_forecast import (
    AdjustedPvForecast,
    ExtrapolatedLive,
    LivePvSignals,
    SolcastPeriod,
    WeatherConditionAtHour,
)
from .pv_forecast_strategy import (
    EXTRAP_STRATEGIES,
    TODAY_STRATEGIES,
    TOMORROW_STRATEGIES,
    ForecastContext,
    PvForecast,
    adjust_pv_forecast_at6,
    adjust_pv_forecast_live,
)

__all__ = [
    "EXTRAP_STRATEGIES",
    "LivePvSignals",
    "PvForecast",
    "PvForecastUpdater",
    "TODAY_STRATEGIES",
    "TOMORROW_STRATEGIES",
]


def _empty_forecasts() -> dict[PvForecast, AdjustedPvForecast | None]:
    """Legacy result cache — populated for unbound variants (TOMORROW × 2 in Iter 1b)."""
    return {
        strategy: None for strategy in PvForecast if strategy not in EXTRAP_STRATEGIES
    }


def _empty_extrapolated() -> dict[PvForecast, ExtrapolatedLive]:
    """Legacy EXTRAP result cache — recomputed each tick by _recompute_legacy_extrap."""
    return {strategy: ExtrapolatedLive.empty() for strategy in EXTRAP_STRATEGIES}


@dataclass
class PvForecastUpdater:
    """Orchestrates PvForecast strategy updates + caches inputs.

    Callers push only what changed via trigger-named methods; the updater
    builds the full `ForecastContext` from cached inputs and dispatches to
    all bound strategies (Iter 1b: AT_6 + LIVE). Unbound variants flow
    through the legacy `_forecasts` / `_extrapolated` dicts until Iter 3.
    """

    # — Cached inputs (rebuilt into ForecastContext per dispatch) —
    _signals: LivePvSignals = field(default_factory=LivePvSignals)
    _weather: list[WeatherConditionAtHour] = field(default_factory=list)
    _solcast_at_6: list[SolcastPeriod] = field(default_factory=list)
    _solcast_live: list[SolcastPeriod] = field(default_factory=list)
    _solcast_tomorrow: list[SolcastPeriod] = field(default_factory=list)
    # — Legacy EXTRAP inputs (Iter 1b transitional — service-pushed) —
    _realized_pv_today: dict[tuple[int, int], float] = field(default_factory=dict)
    _consumption_w: float | None = None
    _start_charge_hour: int | None = None
    # — Legacy result caches (Iter 1b transitional — for unbound variants) —
    _forecasts: dict[PvForecast, AdjustedPvForecast | None] = field(
        default_factory=_empty_forecasts
    )
    _extrapolated: dict[PvForecast, ExtrapolatedLive] = field(
        default_factory=_empty_extrapolated
    )

    # ─── Read API ──────────────────────────────────────────────────────────

    def get(self, variant: PvForecast) -> AdjustedPvForecast | None:
        """Return adjusted forecast for `variant`.

        Bound variants (Iter 1b: AT_6, LIVE) read from `variant.strategy.adjusted`.
        Unbound variants fall back to the legacy `_forecasts` /
        `_extrapolated[variant].adjusted` cache.
        """
        if variant.strategy is not None:
            return variant.adjusted
        if variant in EXTRAP_STRATEGIES:
            return self._extrapolated[variant].adjusted
        return self._forecasts.get(variant)

    def get_extrapolated(self, variant: PvForecast) -> ExtrapolatedLive | None:
        """Return full ExtrapolatedLive bundle for an EXTRAP_* variant.

        Bundles `adjusted` + `remaining_kwh` + `target_soc`. Used by sensors
        that need state/SOC alongside the chart-facing forecast.
        """
        if variant not in EXTRAP_STRATEGIES:
            return None
        return self._extrapolated.get(variant)

    def all(self) -> dict[PvForecast, AdjustedPvForecast | None]:
        """Snapshot dict of every variant → forecast (or None)."""
        return {variant: self.get(variant) for variant in PvForecast}

    def today(self) -> dict[PvForecast, AdjustedPvForecast | None]:
        """Snapshot of today-axis variants (AT_6, LIVE, 4× EXTRAP)."""
        return {v: self.get(v) for v in TODAY_STRATEGIES}

    def tomorrow(self) -> dict[PvForecast, AdjustedPvForecast | None]:
        """Snapshot of tomorrow-axis variants (TOMORROW_AT_6, TOMORROW_LIVE)."""
        return {v: self.get(v) for v in TOMORROW_STRATEGIES}

    @property
    def signals(self) -> LivePvSignals:
        """Read-only snapshot of current PV-side live readings."""
        return self._signals

    @property
    def solcast_live(self) -> list[SolcastPeriod]:
        """Raw Solcast live periods — exposed for downstream consumers."""
        return self._solcast_live

    # ─── Trigger-named public API: each takes only the delta ────────────────

    def today_at_6_forecast_updated(
        self,
        periods: list[SolcastPeriod],
        weather: list[WeatherConditionAtHour],
        now: datetime,
    ) -> None:
        """Solcast at_6 entity changed (~once daily 06:00).

        Iter 1b: AT_6 strategy picks up the new periods + weather and
        rebuilds its `adjusted` via `_dispatch`.
        """
        self._solcast_at_6 = periods
        self._weather = weather
        self._dispatch(now)

    def today_live_forecast_updated(
        self,
        periods: list[SolcastPeriod],
        weather: list[WeatherConditionAtHour],
        now: datetime,
    ) -> None:
        """Solcast live entity changed (continuous updates).

        Iter 1b: LIVE strategy picks up via dispatch; then legacy EXTRAP
        recompute fires (it feeds off LIVE + raw solcast_live).
        """
        self._solcast_live = periods
        self._weather = weather
        self._dispatch(now)
        self._recompute_legacy_extrap(now)

    def tomorrow_forecast_updated(
        self,
        periods: list[SolcastPeriod],
        weather: list[WeatherConditionAtHour],
        now: datetime,
    ) -> None:
        """Solcast tomorrow entity changed.

        Iter 1b: TOMORROW_* strategies are unbound, so this method runs the
        legacy adjust helpers and stashes results into `_forecasts`. Iter
        3 will bind them; this body collapses to `_dispatch` only.
        """
        self._solcast_tomorrow = periods
        self._weather = weather
        # AT6 modifiers serve evening planning safety lower-bound.
        self._forecasts[PvForecast.TOMORROW_AT_6] = adjust_pv_forecast_at6(
            periods, weather
        )
        # LIVE modifiers align with target_soc_live after midnight rollover.
        # adjust_pv_forecast_live checks is_first_hour = (period.hour == now.hour);
        # tomorrow periods (date = tomorrow) never match → all use standard
        # LIVE modifiers (no special first-hour treatment).
        self._forecasts[PvForecast.TOMORROW_LIVE] = adjust_pv_forecast_live(
            periods, weather, now
        )
        self._dispatch(now)

    def weather_updated(
        self,
        weather: list[WeatherConditionAtHour],
        now: datetime,
    ) -> None:
        """Weather forecast changed — re-adjust every variant that depends on it."""
        self._weather = weather
        # Tomorrow variants are unbound in Iter 1b — re-run legacy adjust if
        # we have cached tomorrow Solcast periods.
        if self._solcast_tomorrow:
            self._forecasts[PvForecast.TOMORROW_AT_6] = adjust_pv_forecast_at6(
                self._solcast_tomorrow, weather
            )
            self._forecasts[PvForecast.TOMORROW_LIVE] = adjust_pv_forecast_live(
                self._solcast_tomorrow, weather, now
            )
        self._dispatch(now)
        self._recompute_legacy_extrap(now)

    def live_pv_updated(
        self,
        signals: LivePvSignals,
        now: datetime,
    ) -> None:
        """Per-minute tick — PV-side live signals refreshed.

        Bound strategies re-patch in-progress bucket; legacy EXTRAP
        recomputes (uses signals + cached cons knobs).
        """
        self._signals = signals
        self._dispatch(now)
        self._recompute_legacy_extrap(now)

    def refresh_extrap_inputs(
        self,
        realized_pv_today: dict[tuple[int, int], float],
        consumption_w: float | None,
        start_charge_hour: int | None,
    ) -> None:
        """Push cons-side knobs needed by legacy EXTRAP recompute (Iter 1b).

        EXTRAP variants are unbound in Iter 1b and their pure-function
        recompute needs cons-side inputs that flow through
        `TargetSocCatalog`. Service forwards them here so the updater is
        self-contained per dispatch. Iter 3 removes this — EXTRAP
        strategies will read from `ForecastContext`.
        """
        self._realized_pv_today = realized_pv_today
        self._consumption_w = consumption_w
        self._start_charge_hour = start_charge_hour

    # ─── Internal ──────────────────────────────────────────────────────────

    def _dispatch(self, now: datetime) -> None:
        """Build ctx from cached inputs + dispatch to bound strategies."""
        ctx = ForecastContext(
            now=now,
            signals=self._signals,
            weather=self._weather,
            solcast_at_6=self._solcast_at_6,
            solcast_live=self._solcast_live,
            solcast_tomorrow=self._solcast_tomorrow,
        )
        for variant in PvForecast:
            if variant.strategy is not None:
                variant.strategy.update(ctx)

    def _recompute_legacy_extrap(self, now: datetime) -> None:
        """Recompute 4 EXTRAP variants (Iter 1b legacy path).

        Uses cached LIVE adjusted + raw solcast_live + signals + cons knobs.
        No-op when LIVE forecast not yet computed (early startup race).
        """
        adjusted_live = self.get(PvForecast.LIVE)
        if adjusted_live is None:
            return
        pv_w = self._signals.pv_power_w
        so_far = self._signals.bucket_so_far_kwh
        self._extrapolated[PvForecast.EXTRAP_PATTERN] = (
            pv_forecast_extrapolation.extrapolate_calibrated_pattern(
                adjusted_live,
                self._solcast_live,
                now,
                so_far,
                self._realized_pv_today,
                pv_power_w_5min=pv_w,
                consumption_w=self._consumption_w,
                start_charge_hour=self._start_charge_hour,
            )
        )
        self._extrapolated[PvForecast.EXTRAP_PROPORTIONAL] = (
            pv_forecast_extrapolation.extrapolate_proportional_median(
                adjusted_live,
                self._solcast_live,
                now,
                so_far,
                self._realized_pv_today,
                pv_power_w_5min=pv_w,
                consumption_w=self._consumption_w,
                start_charge_hour=self._start_charge_hour,
            )
        )
        self._extrapolated[PvForecast.EXTRAP_BAND] = (
            pv_forecast_extrapolation.extrapolate_band_clamped(
                adjusted_live,
                self._solcast_live,
                now,
                so_far,
                self._realized_pv_today,
                pv_power_w_5min=pv_w,
                consumption_w=self._consumption_w,
                start_charge_hour=self._start_charge_hour,
            )
        )
        self._extrapolated[PvForecast.EXTRAP_BAND_RECENT] = (
            pv_forecast_extrapolation.extrapolate_band_clamped_recent(
                adjusted_live,
                self._solcast_live,
                now,
                so_far,
                self._realized_pv_today,
                pv_power_w_5min=pv_w,
                consumption_w=self._consumption_w,
                start_charge_hour=self._start_charge_hour,
            )
        )
