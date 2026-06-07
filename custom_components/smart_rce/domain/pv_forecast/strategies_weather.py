"""Weather-adjusted strategies — `At6Strategy` + `LiveStrategy`.

Both consume a Solcast forecast (at_6 snapshot vs. continuous live) and
apply weather-condition-dependent modifiers to each period's hourly
rate. `today=True/False` parameter switches the date axis: today
variants read `ctx.solcast_at_6` / `ctx.solcast_today` and support the
in-progress bucket patch; tomorrow variants read `ctx.solcast_tomorrow`
and skip the patch (no matching today-clock bucket).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from .strategy_base import (
    AdjustedPeriod,
    ForecastContext,
    ForecastStrategy,
    PvForecastResult,
    SolcastPeriod,
)

# --- Weather-condition classification (shared by At6 + Live) --- #

SUNNY_CONDITIONS = frozenset({"sunny", "clear-night"})
PARTLY_VARIABLE_CONDITIONS = frozenset({"partlycloudy-variable"})
PARTLY_CONDITIONS = frozenset({"partlycloudy"})
# Everything else = cloudy/other


# --- At6Strategy --- #


class At6Strategy(ForecastStrategy):
    """Morning Solcast snapshot — pessimistic AT6 weather modifiers.

    `today=True` reads `ctx.solcast_at_6` and supports in-progress
    patch. `today=False` reads `ctx.solcast_tomorrow` and skips
    in-progress patch (no matching bucket on today's clock).
    """

    # Max hourly rate at hour 7 for cloudy conditions (kWh/h cap).
    _CLOUDY_CAP_HOUR_7: Final[float] = 0.20
    # AT6 cloudy modifiers per hour (hourly rate multiplier on pv_estimate10).
    _CLOUDY_MODIFIER_EARLY: Final[float] = 0.5  # hours 7-10
    _CLOUDY_MODIFIER_LATE: Final[float] = 0.7  # hours 11+

    def __init__(self, today: bool = True) -> None:
        super().__init__()
        self.is_today = today
        self.supports_in_progress_patch = today

    @property
    def pretty_label(self) -> str:
        return "At 6" if self.is_today else "Tomorrow At 6"

    def _compute(self, ctx: ForecastContext) -> PvForecastResult | None:
        periods = ctx.solcast_at_6 if self.is_today else ctx.solcast_tomorrow
        if not periods or not ctx.weather:
            return None
        return self._adjust_forecast(periods, ctx)

    @staticmethod
    def _adjust_forecast(
        solcast_periods: list[SolcastPeriod], ctx: ForecastContext
    ) -> PvForecastResult:
        forecast: list[AdjustedPeriod] = []
        total_kwh = 0.0
        for period in solcast_periods:
            dt = datetime.fromisoformat(period.period_start)
            condition = ctx.weather.for_hour(dt.hour, dt.date())
            adj_rate = At6Strategy._adjust_period(period, condition, dt.hour)
            forecast.append(
                AdjustedPeriod(
                    period_start=period.period_start,
                    pv_estimate_adjusted=round(adj_rate, 4),
                )
            )
            total_kwh += adj_rate / 2
        return PvForecastResult(forecast=forecast, total_kwh=round(total_kwh, 4))

    @staticmethod
    def _adjust_period(period: SolcastPeriod, condition: str, hour: int) -> float:
        """Apply AT6 weather adjustment. Returns adjusted hourly rate."""
        cat = _classify_condition(condition)
        if cat == "sunny":
            return period.pv_estimate * 1.0
        if cat == "partly-variable":
            return period.pv_estimate * 0.8
        if cat == "partly":
            return period.pv_estimate * 0.7
        # cloudy/other
        modifier = (
            At6Strategy._CLOUDY_MODIFIER_EARLY
            if hour <= 10
            else At6Strategy._CLOUDY_MODIFIER_LATE
        )
        adj = period.pv_estimate10 * modifier
        if hour == 7:
            adj = min(adj, At6Strategy._CLOUDY_CAP_HOUR_7)
        return adj


# --- LiveStrategy --- #


class LiveStrategy(ForecastStrategy):
    """Continuous Solcast updates — optimistic LIVE modifiers + first-hour trust.

    `today=True` reads `ctx.solcast_today`. `today=False` reads
    `ctx.solcast_tomorrow`. The `is_first_hour` check inside
    `_adjust_period` compares period.hour to `ctx.now.hour`;
    tomorrow's periods (different date) never match → all use
    standard LIVE modifiers (no special first-hour treatment).
    """

    def __init__(self, today: bool = True) -> None:
        super().__init__()
        self.is_today = today
        self.supports_in_progress_patch = today

    @property
    def pretty_label(self) -> str:
        return "Live" if self.is_today else "Tomorrow Live"

    def _compute(self, ctx: ForecastContext) -> PvForecastResult | None:
        periods = ctx.solcast_today if self.is_today else ctx.solcast_tomorrow
        if not periods or not ctx.weather:
            return None
        return self._adjust_forecast(periods, ctx)

    @staticmethod
    def _adjust_forecast(
        solcast_periods: list[SolcastPeriod], ctx: ForecastContext
    ) -> PvForecastResult:
        forecast: list[AdjustedPeriod] = []
        total_kwh = 0.0
        current_hour = ctx.now.hour
        for period in solcast_periods:
            dt = datetime.fromisoformat(period.period_start)
            is_first_hour = dt.hour == current_hour
            condition = ctx.weather.for_hour(dt.hour, dt.date())
            adj_rate = LiveStrategy._adjust_period(period, condition, is_first_hour)
            forecast.append(
                AdjustedPeriod(
                    period_start=period.period_start,
                    pv_estimate_adjusted=round(adj_rate, 4),
                )
            )
            total_kwh += adj_rate / 2
        return PvForecastResult(forecast=forecast, total_kwh=round(total_kwh, 4))

    @staticmethod
    def _adjust_period(
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


# --- Shared helper (used by At6Strategy + LiveStrategy adjust_period) --- #


def _classify_condition(condition: str) -> str:
    """Classify weather condition into: sunny, partly-variable, partly, cloudy."""
    if condition in SUNNY_CONDITIONS:
        return "sunny"
    if condition in PARTLY_VARIABLE_CONDITIONS:
        return "partly-variable"
    if condition in PARTLY_CONDITIONS:
        return "partly"
    return "cloudy"
