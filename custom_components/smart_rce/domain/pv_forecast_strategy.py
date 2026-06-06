"""Forecast strategy hierarchy + `PvForecast` enum that binds strategies.

Each PV forecast variant is a `PvForecast` enum member with a bound
stateful `ForecastStrategy` instance (HeaterState-style). Template
method `ForecastStrategy.update(ctx)`:
1. Subclass `_compute(ctx)` builds fresh adjusted from ctx, or None when
   the relevant input is missing
2. Cache non-None result on `self.adjusted`
3. If `supports_in_progress_patch=True`, re-patch with live signals
   (today-variants only — tomorrow has no in-progress bucket)

Iter 1b: AT_6 + LIVE bound; the other 6 variants have `strategy=None`
(legacy path via `PvForecastUpdater._forecasts` / `_extrapolated` dicts).
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
    AdjustedPvForecast,
    LivePvSignals,
    SolcastPeriod,
    WeatherConditionAtHour,
)

# --- Strategy base + context VO --- #


@dataclass(frozen=True)
class ForecastContext:
    """Inputs available to strategy updates per dispatch.

    `PvForecastUpdater` caches all inputs and rebuilds the full context
    each time something changes. Strategies short-circuit in `_compute`
    when their relevant input is missing (e.g. AT_6 needs
    `solcast_at_6` + `weather`).
    """

    now: datetime
    signals: LivePvSignals
    weather: list[WeatherConditionAtHour] = field(default_factory=list)
    solcast_at_6: list[SolcastPeriod] = field(default_factory=list)
    solcast_live: list[SolcastPeriod] = field(default_factory=list)
    solcast_tomorrow: list[SolcastPeriod] = field(default_factory=list)


class ForecastStrategy:
    """Base for PV forecast scenarios. Template-method `update()`.

    Subclasses override `_compute(ctx)` to build a fresh adjusted forecast
    from ctx (or None when relevant input missing). update() caches the
    result + optionally re-patches with live signals for today-variants.
    """

    # Today-variants set True (in-progress bucket needs live-signal refresh
    # each tick). Tomorrow-variants leave default False — no matching
    # in-progress bucket on today's clock.
    supports_in_progress_patch: bool = False

    def __init__(self) -> None:
        self.adjusted: AdjustedPvForecast | None = None

    def update(self, ctx: ForecastContext) -> None:
        new_adjusted = self._compute(ctx)
        if new_adjusted is not None:
            self.adjusted = new_adjusted
        if self.supports_in_progress_patch and self.adjusted is not None:
            self.adjusted = _apply_chart_in_progress_patch(
                ctx.now, self.adjusted, ctx.signals
            )

    def _compute(self, ctx: ForecastContext) -> AdjustedPvForecast | None:
        """Subclass: build fresh adjusted from ctx, or None if input missing."""
        raise NotImplementedError


class At6Strategy(ForecastStrategy):
    """Morning Solcast snapshot (~06:05) — pessimistic AT6 weather modifiers."""

    supports_in_progress_patch = True

    def _compute(self, ctx: ForecastContext) -> AdjustedPvForecast | None:
        if not ctx.solcast_at_6 or not ctx.weather:
            return None
        return _adjust_pv_forecast_at6(ctx.solcast_at_6, ctx.weather)


class LiveStrategy(ForecastStrategy):
    """Continuous Solcast updates — optimistic LIVE modifiers + first-hour trust."""

    supports_in_progress_patch = True

    def _compute(self, ctx: ForecastContext) -> AdjustedPvForecast | None:
        if not ctx.solcast_live or not ctx.weather:
            return None
        return _adjust_pv_forecast_live(ctx.solcast_live, ctx.weather, ctx.now)


# --- PvForecast enum (variants + bound strategies) --- #


class PvForecast(Enum):
    """All PV forecast variants — string key + bound strategy.

    Iter 1b: only AT_6 + LIVE bound. Other 6 have `strategy=None` —
    `PvForecastUpdater` handles them via its legacy `_forecasts` /
    `_extrapolated` dicts. Iter 3 binds the remaining 6.

    Naming convention: `<date_axis>_<source>` where source ∈ {at_6, live,
    extrap_*}. Today's variants drop the date prefix (implicit).
    """

    AT_6 = ("at_6", At6Strategy())
    LIVE = ("live", LiveStrategy())
    TOMORROW_AT_6 = ("tomorrow_at_6", None)
    TOMORROW_LIVE = ("tomorrow_live", None)
    EXTRAP_PATTERN = ("extrapolated_live_pattern", None)
    EXTRAP_PROPORTIONAL = ("extrapolated_live_proportional", None)
    EXTRAP_BAND = ("extrapolated_live_band", None)
    EXTRAP_BAND_RECENT = ("extrapolated_live_band_recent", None)

    def __init__(self, key: str, strategy: ForecastStrategy | None) -> None:
        self.key = key
        self.strategy = strategy

    @property
    def adjusted(self) -> AdjustedPvForecast | None:
        """Current adjusted forecast — from bound strategy (None if unbound)."""
        return self.strategy.adjusted if self.strategy is not None else None


TODAY_STRATEGIES: Final[tuple[PvForecast, ...]] = (
    PvForecast.AT_6,
    PvForecast.LIVE,
    PvForecast.EXTRAP_PATTERN,
    PvForecast.EXTRAP_PROPORTIONAL,
    PvForecast.EXTRAP_BAND,
    PvForecast.EXTRAP_BAND_RECENT,
)

TOMORROW_STRATEGIES: Final[tuple[PvForecast, ...]] = (
    PvForecast.TOMORROW_AT_6,
    PvForecast.TOMORROW_LIVE,
)

EXTRAP_STRATEGIES: Final[tuple[PvForecast, ...]] = (
    PvForecast.EXTRAP_PATTERN,
    PvForecast.EXTRAP_PROPORTIONAL,
    PvForecast.EXTRAP_BAND,
    PvForecast.EXTRAP_BAND_RECENT,
)


# --- Module-level adjust helpers (relocated from PvForecastUpdater) --- #
# Also reused by `PvForecastUpdater.tomorrow_forecast_updated` (legacy
# path in Iter 1b) until Iter 3 binds TOMORROW strategies.


def _apply_chart_in_progress_patch(
    now: datetime,
    adjusted: AdjustedPvForecast,
    signals: LivePvSignals,
) -> AdjustedPvForecast:
    """Return `adjusted` with in-progress period rescaled, or unchanged.

    Rescale = full-bucket estimate (realized so-far + remaining via 5-min
    power); unchanged when live signals aren't set.
    """
    pv_w = signals.pv_power_w
    so_far = signals.bucket_so_far_kwh
    if pv_w is None or so_far is None:
        return adjusted
    return adjusted.with_now_aware_in_progress(
        now=now, pv_power_w_5min=pv_w, pv_bucket_so_far_kwh=so_far
    )


def adjust_pv_forecast_at6(
    solcast_periods: list[SolcastPeriod],
    weather_conditions: list[WeatherConditionAtHour],
) -> AdjustedPvForecast:
    """Adjust morning Solcast forecast (snapshot from 6:05) using weather."""
    return _adjust_pv_forecast_at6(solcast_periods, weather_conditions)


def adjust_pv_forecast_live(
    solcast_periods: list[SolcastPeriod],
    weather_conditions: list[WeatherConditionAtHour],
    now: datetime,
) -> AdjustedPvForecast:
    """Adjust live Solcast forecast using weather. First hour treated differently."""
    return _adjust_pv_forecast_live(solcast_periods, weather_conditions, now)


def _adjust_pv_forecast_at6(
    solcast_periods: list[SolcastPeriod],
    weather_conditions: list[WeatherConditionAtHour],
) -> AdjustedPvForecast:
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
    return AdjustedPvForecast(forecast=forecast, total_kwh=round(total_kwh, 4))


def _adjust_pv_forecast_live(
    solcast_periods: list[SolcastPeriod],
    weather_conditions: list[WeatherConditionAtHour],
    now: datetime,
) -> AdjustedPvForecast:
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
    return AdjustedPvForecast(forecast=forecast, total_kwh=round(total_kwh, 4))


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
