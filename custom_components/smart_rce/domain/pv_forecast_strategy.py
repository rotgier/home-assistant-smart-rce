"""Forecast strategy hierarchy + `PvForecast` enum that binds strategies.

Each PV forecast variant is a `PvForecast` enum member with a bound
stateful `ForecastStrategy` instance (HeaterState-style). Template
method `ForecastStrategy.update(ctx)`:
1. Subclass `_compute(ctx)` builds fresh adjusted from ctx, or None when
   the relevant input is missing
2. Cache non-None result on `self.result`
3. If `supports_in_progress_patch=True`, re-patch with live signals
   (today-variants only — tomorrow has no in-progress bucket)

Iter 1b: AT_6 + LIVE bound; the other 6 variants have `strategy=None`
(legacy path via `PvForecasts._forecasts` / `_extrapolated` dicts).
Iter 3 will bind the remaining 6.

PvForecast enum lives here (not in `pv_forecast.py`) because the enum
members bind strategy instances — co-locating avoids circular imports
between the enum and the strategy classes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Final

from .pv_forecast import (
    AT6_CLOUDY_MODIFIER_EARLY,
    AT6_CLOUDY_MODIFIER_LATE,
    CLOUDY_CAP_HOUR_7,
    PARTLY_CONDITIONS,
    PARTLY_VARIABLE_CONDITIONS,
    SUNNY_CONDITIONS,
    AdjustedPeriod,
    LivePvSignals,
    PvForecastResult,
    SolcastPeriod,
    WeatherConditionAtHour,
)
from .pv_forecast_extrapolation import (
    extrapolate_band_clamped,
    extrapolate_band_clamped_recent,
    extrapolate_calibrated_pattern,
    extrapolate_proportional_median,
)

# --- Strategy base + context VO --- #


@dataclass(frozen=True)
class ForecastContext:
    """Inputs available to strategy updates per dispatch.

    `PvForecasts` caches all inputs and rebuilds the full context
    each time something changes. Strategies short-circuit in `_compute`
    when their relevant input is missing (e.g. AT_6 needs
    `solcast_at_6` + `weather`).

    `realized_pv_today`, `consumption_w`, `start_charge_hour` (added
    Iter 3b) are EXTRAP strategy inputs — realized PV history from
    recorder + cons-side knobs forwarded from `TargetSocCatalog.inputs`.
    """

    now: datetime
    signals: LivePvSignals
    weather: list[WeatherConditionAtHour] = field(default_factory=list)
    solcast_at_6: list[SolcastPeriod] = field(default_factory=list)
    solcast_today: list[SolcastPeriod] = field(default_factory=list)
    solcast_tomorrow: list[SolcastPeriod] = field(default_factory=list)
    realized_pv_today: dict[tuple[int, int], float] = field(default_factory=dict)
    consumption_w: float | None = None
    start_charge_hour: int | None = None


class ForecastStrategy:
    """Base for PV forecast scenarios. Template-method `update()`.

    Subclasses override `_compute(ctx)` to build a fresh forecast result
    from ctx (or None when relevant input missing). `update()` caches
    the result, optionally re-patches in-progress bucket with live
    signals (today-variants only), and derives `remaining_kwh`.

    Unifying contract (Iter 3b): every strategy exposes
    `result: PvForecastResult | None` + `total_kwh: float | None`
    (forwarded property) + `remaining_kwh: float | None` (scalar
    derived from `result` per tick).
    """

    # Today-variants set True (in-progress bucket needs live-signal refresh
    # each tick). Tomorrow-variants leave default False — no matching
    # in-progress bucket on today's clock.
    supports_in_progress_patch: bool = False

    def __init__(self) -> None:
        self.result: PvForecastResult | None = None
        self.remaining_kwh: float | None = None

    @property
    def total_kwh(self) -> float | None:
        """Forwarded from `result.total_kwh` — None when no result yet."""
        return self.result.total_kwh if self.result is not None else None

    def update(self, ctx: ForecastContext) -> None:
        new_result = self._compute(ctx)
        if new_result is not None:
            self.result = new_result
        if self.supports_in_progress_patch and self.result is not None:
            self.result = _apply_chart_in_progress_patch(
                ctx.now, self.result, ctx.signals
            )
        self.remaining_kwh = self._derive_remaining_kwh(ctx)

    def _compute(self, ctx: ForecastContext) -> PvForecastResult | None:
        """Subclass: build fresh adjusted from ctx, or None if input missing."""
        raise NotImplementedError

    def _derive_remaining_kwh(self, ctx: ForecastContext) -> float | None:
        """Sum kWh from `ctx.now` onwards on the result's date axis.

        Default impl reuses `PvForecastResult.remaining_kwh_from(now)`
        which filters periods by date + bucket-start time. Tomorrow-axis
        results naturally sum the whole window (all periods are on a
        later date than `now`). EXTRAP strategies inherit this default —
        their post-`_assemble` result already has in-progress rescaled,
        so the sum matches the legacy `ExtrapolatedLive.remaining_kwh`
        value exactly.
        """
        if self.result is None:
            return None
        return self.result.remaining_kwh_from(ctx.now)


class At6Strategy(ForecastStrategy):
    """Morning Solcast snapshot — pessimistic AT6 weather modifiers.

    `today=True` reads `ctx.solcast_at_6` (today's morning snapshot) and
    supports in-progress patch. `today=False` reads `ctx.solcast_tomorrow`
    (Solcast publishes tomorrow as a separate entity) and skips
    in-progress patch — no matching bucket on today's clock.
    """

    def __init__(self, today: bool = True) -> None:
        super().__init__()
        self._today = today
        self.supports_in_progress_patch = today

    def _compute(self, ctx: ForecastContext) -> PvForecastResult | None:
        periods = ctx.solcast_at_6 if self._today else ctx.solcast_tomorrow
        if not periods or not ctx.weather:
            return None
        return _adjust_pv_forecast_at6(periods, ctx.weather)


class LiveStrategy(ForecastStrategy):
    """Continuous Solcast updates — optimistic LIVE modifiers + first-hour trust.

    `today=True` reads `ctx.solcast_today`. `today=False` reads
    `ctx.solcast_tomorrow`. The `is_first_hour` check inside
    `_adjust_pv_forecast_live` compares period.hour to `ctx.now.hour`;
    tomorrow's periods (different date) never match → all use standard
    LIVE modifiers (no special first-hour treatment).
    """

    def __init__(self, today: bool = True) -> None:
        super().__init__()
        self._today = today
        self.supports_in_progress_patch = today

    def _compute(self, ctx: ForecastContext) -> PvForecastResult | None:
        periods = ctx.solcast_today if self._today else ctx.solcast_tomorrow
        if not periods or not ctx.weather:
            return None
        return _adjust_pv_forecast_live(periods, ctx.weather, ctx.now)


# --- EXTRAP strategies (Iter 3b) --- #
#
# Four today-axis variants that extrapolate LIVE based on realized PV
# history per bucket. All share input plumbing (read LIVE result + raw
# `solcast_today` + signals + realized_pv from ctx); they differ only in
# the projection algorithm. `_assemble` in `pv_forecast_extrapolation`
# already handles in-progress patch + future overrides, so
# `supports_in_progress_patch=False` here.


class _ExtrapStrategyBase(ForecastStrategy):
    """Base for EXTRAP variants — extrapolates LIVE on realized PV history.

    Each subclass implements `_run_extrapolation(ctx)` by calling its
    corresponding `extrapolate_*` function from `pv_forecast_extrapolation`.
    Returns `PvForecastResult` or None when inputs are insufficient.
    """

    supports_in_progress_patch = False

    def _compute(self, ctx: ForecastContext) -> PvForecastResult | None:
        pv_forecast_live = PvForecast.LIVE.result
        if pv_forecast_live is None or not ctx.solcast_today:
            return None
        return self._run_extrapolation(ctx, pv_forecast_live)

    def _run_extrapolation(
        self, ctx: ForecastContext, pv_forecast_live: PvForecastResult
    ) -> PvForecastResult | None:
        """Subclass: call its own `extrapolate_*` function."""
        raise NotImplementedError


class ExtrapPatternStrategy(_ExtrapStrategyBase):
    """4-zone weighted realization-score pattern (calibrated)."""

    def _run_extrapolation(self, ctx, pv_forecast_live):
        return extrapolate_calibrated_pattern(
            pv_forecast_live,
            ctx.solcast_today,
            ctx.now,
            ctx.signals.bucket_so_far_kwh,
            ctx.realized_pv_today,
            pv_power_w_5min=ctx.signals.pv_power_w,
        )


class ExtrapProportionalStrategy(_ExtrapStrategyBase):
    """Proportional median — band-width independent `(real-est)/est` score."""

    def _run_extrapolation(self, ctx, pv_forecast_live):
        return extrapolate_proportional_median(
            pv_forecast_live,
            ctx.solcast_today,
            ctx.now,
            ctx.signals.bucket_so_far_kwh,
            ctx.realized_pv_today,
            pv_power_w_5min=ctx.signals.pv_power_w,
        )


class ExtrapBandStrategy(_ExtrapStrategyBase):
    """2-zone band-clamped score anchored at [p10, p90]."""

    def _run_extrapolation(self, ctx, pv_forecast_live):
        return extrapolate_band_clamped(
            pv_forecast_live,
            ctx.solcast_today,
            ctx.now,
            ctx.signals.bucket_so_far_kwh,
            ctx.realized_pv_today,
            pv_power_w_5min=ctx.signals.pv_power_w,
        )


class ExtrapBandRecentStrategy(_ExtrapStrategyBase):
    """Band-clamped with narrowed recent-only lookback."""

    def _run_extrapolation(self, ctx, pv_forecast_live):
        return extrapolate_band_clamped_recent(
            pv_forecast_live,
            ctx.solcast_today,
            ctx.now,
            ctx.signals.bucket_so_far_kwh,
            ctx.realized_pv_today,
            pv_power_w_5min=ctx.signals.pv_power_w,
        )


# --- PvForecast enum (variants + bound strategies) --- #


class PvForecast(Enum):
    """All PV forecast variants — string key + bound strategy + date axis.

    Iter 3a: AT_6 + LIVE + TOMORROW_AT_6 + TOMORROW_LIVE bound (At6/Live
    strategies parameterized via `today` flag — DRY). EXTRAP × 4 still
    have `strategy=None` (Iter 3b binds them).

    Naming convention: `<date_axis>_<source>` where source ∈ {at_6, live,
    extrap_*}. Today's variants drop the date prefix (implicit). The
    `is_today` flag is declared at the source — consumers iterate via
    `[v for v in PvForecast if v.is_today]` or use the derived
    `TODAY_STRATEGIES` / `TOMORROW_STRATEGIES` tuples below.
    """

    AT_6 = ("at_6", At6Strategy(today=True), True)
    LIVE = ("live", LiveStrategy(today=True), True)
    TOMORROW_AT_6 = ("tomorrow_at_6", At6Strategy(today=False), False)
    TOMORROW_LIVE = ("tomorrow_live", LiveStrategy(today=False), False)
    EXTRAP_PATTERN = ("extrapolated_live_pattern", ExtrapPatternStrategy(), True)
    EXTRAP_PROPORTIONAL = (
        "extrapolated_live_proportional",
        ExtrapProportionalStrategy(),
        True,
    )
    EXTRAP_BAND = ("extrapolated_live_band", ExtrapBandStrategy(), True)
    EXTRAP_BAND_RECENT = (
        "extrapolated_live_band_recent",
        ExtrapBandRecentStrategy(),
        True,
    )

    def __init__(
        self, key: str, strategy: ForecastStrategy | None, is_today: bool
    ) -> None:
        self.key = key
        self.strategy = strategy
        self.is_today = is_today

    @property
    def is_tomorrow(self) -> bool:
        return not self.is_today

    @property
    def result(self) -> PvForecastResult | None:
        """Current forecast result — from bound strategy (None if unbound)."""
        return self.strategy.result if self.strategy is not None else None


TODAY_STRATEGIES: Final[tuple[PvForecast, ...]] = tuple(
    v for v in PvForecast if v.is_today
)
TOMORROW_STRATEGIES: Final[tuple[PvForecast, ...]] = tuple(
    v for v in PvForecast if v.is_tomorrow
)

# EXTRAP — separate axis (source/computation kind, not date axis).
# Hardcoded list — Iter 3 will introduce ForecastStrategy-bound EXTRAP and
# this tuple may also become derivable via an `is_extrap` flag.
EXTRAP_STRATEGIES: Final[tuple[PvForecast, ...]] = (
    PvForecast.EXTRAP_PATTERN,
    PvForecast.EXTRAP_PROPORTIONAL,
    PvForecast.EXTRAP_BAND,
    PvForecast.EXTRAP_BAND_RECENT,
)


# --- Module-level adjust helpers (relocated from PvForecasts) --- #
# Also reused by `PvForecasts.solcast_tomorrow_updated` (legacy
# path in Iter 1b) until Iter 3 binds TOMORROW strategies.


def _apply_chart_in_progress_patch(
    now: datetime,
    result: PvForecastResult,
    signals: LivePvSignals,
) -> PvForecastResult:
    """Return `result` with in-progress period rescaled, or unchanged.

    Rescale = full-bucket estimate (realized so-far + remaining via 5-min
    power); unchanged when live signals aren't set.
    """
    pv_w = signals.pv_power_w
    so_far = signals.bucket_so_far_kwh
    if pv_w is None or so_far is None:
        return result
    return result.with_now_aware_in_progress(
        now=now, pv_power_w_5min=pv_w, pv_bucket_so_far_kwh=so_far
    )


def adjust_pv_forecast_at6(
    solcast_periods: list[SolcastPeriod],
    weather_conditions: list[WeatherConditionAtHour],
) -> PvForecastResult:
    """Adjust morning Solcast forecast (snapshot from 6:05) using weather."""
    return _adjust_pv_forecast_at6(solcast_periods, weather_conditions)


def adjust_pv_forecast_live(
    solcast_periods: list[SolcastPeriod],
    weather_conditions: list[WeatherConditionAtHour],
    now: datetime,
) -> PvForecastResult:
    """Adjust live Solcast forecast using weather. First hour treated differently."""
    return _adjust_pv_forecast_live(solcast_periods, weather_conditions, now)


def _adjust_pv_forecast_at6(
    solcast_periods: list[SolcastPeriod],
    weather_conditions: list[WeatherConditionAtHour],
) -> PvForecastResult:
    forecast: list[AdjustedPeriod] = []
    total_kwh = 0.0
    for period in solcast_periods:
        dt = datetime.fromisoformat(period.period_start)
        hour = dt.hour
        target_date = dt.date()
        condition = _get_condition_for_hour(hour, weather_conditions, target_date)
        adj_rate = _adjust_at6_period(period, condition, hour)
        forecast.append(
            AdjustedPeriod(
                period_start=period.period_start,
                pv_estimate_adjusted=round(adj_rate, 4),
            )
        )
        total_kwh += adj_rate / 2
    return PvForecastResult(forecast=forecast, total_kwh=round(total_kwh, 4))


def _adjust_pv_forecast_live(
    solcast_periods: list[SolcastPeriod],
    weather_conditions: list[WeatherConditionAtHour],
    now: datetime,
) -> PvForecastResult:
    forecast: list[AdjustedPeriod] = []
    total_kwh = 0.0
    current_hour = now.hour
    for period in solcast_periods:
        dt = datetime.fromisoformat(period.period_start)
        hour = dt.hour
        target_date = dt.date()
        is_first_hour = hour == current_hour
        condition = _get_condition_for_hour(hour, weather_conditions, target_date)
        adj_rate = _adjust_live_period(period, condition, is_first_hour)
        forecast.append(
            AdjustedPeriod(
                period_start=period.period_start,
                pv_estimate_adjusted=round(adj_rate, 4),
            )
        )
        total_kwh += adj_rate / 2
    return PvForecastResult(forecast=forecast, total_kwh=round(total_kwh, 4))


def _classify_condition(condition: str) -> str:
    """Classify condition into: sunny, partly-variable, partly, cloudy."""
    if condition in SUNNY_CONDITIONS:
        return "sunny"
    if condition in PARTLY_VARIABLE_CONDITIONS:
        return "partly-variable"
    if condition in PARTLY_CONDITIONS:
        return "partly"
    return "cloudy"


def _get_condition_for_hour(
    hour: int,
    weather_conditions: list[WeatherConditionAtHour],
    target_date: date | None = None,
) -> str:
    """Find weather condition for given hour and date. Fallback to cloudy."""
    if target_date:
        for w in weather_conditions:
            if w.forecast_date == target_date and w.hour == hour:
                return w.condition_custom
    for w in weather_conditions:
        if w.hour == hour and w.forecast_date is None:
            return w.condition_custom
    return "cloudy"


def _adjust_at6_period(period: SolcastPeriod, condition: str, hour: int) -> float:
    """Apply AT6 weather adjustment. Returns adjusted hourly rate."""
    cat = _classify_condition(condition)
    if cat == "sunny":
        return period.pv_estimate * 1.0
    if cat == "partly-variable":
        return period.pv_estimate * 0.8
    if cat == "partly":
        return period.pv_estimate * 0.7
    # cloudy/other
    modifier = AT6_CLOUDY_MODIFIER_EARLY if hour <= 10 else AT6_CLOUDY_MODIFIER_LATE
    adj = period.pv_estimate10 * modifier
    if hour == 7:
        adj = min(adj, CLOUDY_CAP_HOUR_7)
    return adj


def _adjust_live_period(
    period: SolcastPeriod, condition: str, is_first_hour: bool
) -> float:
    """Apply LIVE weather adjustment. Returns adjusted hourly rate."""
    cat = _classify_condition(condition)
    if is_first_hour:
        if cat == "cloudy":
            return period.pv_estimate10 * 1.0
        return period.pv_estimate * 1.0
    if cat == "sunny":
        return period.pv_estimate * 1.0
    if cat == "partly-variable":
        return period.pv_estimate * 0.8
    if cat == "partly":
        return period.pv_estimate * 0.7
    return period.pv_estimate10 * 1.0
