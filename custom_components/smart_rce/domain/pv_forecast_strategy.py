"""`PvForecast` enum + `ForecastStrategy` hierarchy."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

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


@dataclass(frozen=True)
class ForecastContext:
    """Inputs available to strategy updates per dispatch."""

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
    """Template-method base. Subclasses override `_compute(ctx)`.

    `update()` caches the result, optionally re-patches in-progress
    bucket with live signals (today-variants only), and derives
    `remaining_kwh`. Every strategy exposes the unifying contract
    `(result, total_kwh, remaining_kwh)`.
    """

    supports_in_progress_patch: bool = False

    def __init__(self) -> None:
        self.result: PvForecastResult | None = None
        self.remaining_kwh: float | None = None

    @property
    def total_kwh(self) -> float | None:
        return self.result.total_kwh if self.result is not None else None

    def update(self, ctx: ForecastContext) -> None:
        new_result = self._compute(ctx)
        if new_result is not None:
            self.result = new_result
        if self.supports_in_progress_patch and self.result is not None:
            self.result = self._apply_chart_in_progress_patch(
                ctx.now, self.result, ctx.signals
            )
        self.remaining_kwh = self._derive_remaining_kwh(ctx)

    def _compute(self, ctx: ForecastContext) -> PvForecastResult | None:
        """Subclass: build fresh result from ctx, or None if input missing."""
        raise NotImplementedError

    def _derive_remaining_kwh(self, ctx: ForecastContext) -> float | None:
        """Sum kWh from `ctx.now` onwards on the result's date axis.

        Default impl reuses `PvForecastResult.remaining_kwh_from(now)`.
        Tomorrow-axis results naturally sum the whole window (all
        periods are on a later date than `now`). EXTRAP strategies
        inherit this default — their post-`_assemble` result already
        has in-progress rescaled, so the sum matches the legacy
        `ExtrapolatedLive.remaining_kwh` value exactly.
        """
        if self.result is None:
            return None
        return self.result.remaining_kwh_from(ctx.now)

    @staticmethod
    def _apply_chart_in_progress_patch(
        now: datetime,
        result: PvForecastResult,
        signals: LivePvSignals,
    ) -> PvForecastResult:
        """Rescale in-progress period to full-bucket estimate (no-op when signals missing)."""
        pv_w = signals.pv_power_w
        so_far = signals.bucket_so_far_kwh
        if pv_w is None or so_far is None:
            return result
        return result.with_now_aware_in_progress(
            now=now, pv_power_w_5min=pv_w, pv_bucket_so_far_kwh=so_far
        )


class At6Strategy(ForecastStrategy):
    """Morning Solcast snapshot — pessimistic AT6 weather modifiers.

    `today=True` reads `ctx.solcast_at_6` and supports in-progress
    patch. `today=False` reads `ctx.solcast_tomorrow` and skips
    in-progress patch (no matching bucket on today's clock).
    """

    def __init__(self, today: bool = True) -> None:
        super().__init__()
        self._today = today
        self.supports_in_progress_patch = today

    def _compute(self, ctx: ForecastContext) -> PvForecastResult | None:
        periods = ctx.solcast_at_6 if self._today else ctx.solcast_tomorrow
        if not periods or not ctx.weather:
            return None
        return self._adjust_forecast(periods, ctx.weather)

    @staticmethod
    def _adjust_forecast(
        solcast_periods: list[SolcastPeriod],
        weather_conditions: list[WeatherConditionAtHour],
    ) -> PvForecastResult:
        forecast: list[AdjustedPeriod] = []
        total_kwh = 0.0
        for period in solcast_periods:
            dt = datetime.fromisoformat(period.period_start)
            condition = _get_condition_for_hour(dt.hour, weather_conditions, dt.date())
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
        modifier = AT6_CLOUDY_MODIFIER_EARLY if hour <= 10 else AT6_CLOUDY_MODIFIER_LATE
        adj = period.pv_estimate10 * modifier
        if hour == 7:
            adj = min(adj, CLOUDY_CAP_HOUR_7)
        return adj


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
        self._today = today
        self.supports_in_progress_patch = today

    def _compute(self, ctx: ForecastContext) -> PvForecastResult | None:
        periods = ctx.solcast_today if self._today else ctx.solcast_tomorrow
        if not periods or not ctx.weather:
            return None
        return self._adjust_forecast(periods, ctx.weather, ctx.now)

    @staticmethod
    def _adjust_forecast(
        solcast_periods: list[SolcastPeriod],
        weather_conditions: list[WeatherConditionAtHour],
        now: datetime,
    ) -> PvForecastResult:
        forecast: list[AdjustedPeriod] = []
        total_kwh = 0.0
        current_hour = now.hour
        for period in solcast_periods:
            dt = datetime.fromisoformat(period.period_start)
            is_first_hour = dt.hour == current_hour
            condition = _get_condition_for_hour(dt.hour, weather_conditions, dt.date())
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


class _ExtrapStrategyBase(ForecastStrategy):
    """Base for EXTRAP variants — extrapolates LIVE on realized PV history.

    `_assemble` in `pv_forecast_extrapolation` already handles
    in-progress patch + future overrides → `supports_in_progress_patch=False`.
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


class PvForecast(Enum):
    """All PV forecast variants — key + bound strategy + axis flags.

    Each member declares at the source: string key, ForecastStrategy
    instance, `is_today` (date axis), `is_extrap` (source/computation
    kind). Consumers iterate partitions via `PvForecast.today()` /
    `.tomorrow()` / `.extrap()` classmethods.

    Naming convention: `<date_axis>_<source>` where source ∈ {at_6, live,
    extrap_*}. Today's variants drop the date prefix (implicit).
    """

    AT_6 = ("at_6", At6Strategy(today=True), True, False)
    LIVE = ("live", LiveStrategy(today=True), True, False)
    TOMORROW_AT_6 = ("tomorrow_at_6", At6Strategy(today=False), False, False)
    TOMORROW_LIVE = ("tomorrow_live", LiveStrategy(today=False), False, False)
    EXTRAP_PATTERN = ("extrapolated_live_pattern", ExtrapPatternStrategy(), True, True)
    EXTRAP_PROPORTIONAL = (
        "extrapolated_live_proportional",
        ExtrapProportionalStrategy(),
        True,
        True,
    )
    EXTRAP_BAND = ("extrapolated_live_band", ExtrapBandStrategy(), True, True)
    EXTRAP_BAND_RECENT = (
        "extrapolated_live_band_recent",
        ExtrapBandRecentStrategy(),
        True,
        True,
    )

    def __init__(
        self,
        key: str,
        strategy: ForecastStrategy,
        is_today: bool,
        is_extrap: bool,
    ) -> None:
        self.key = key
        self.strategy = strategy
        self.is_today = is_today
        self.is_extrap = is_extrap

    @property
    def is_tomorrow(self) -> bool:
        return not self.is_today

    @property
    def result(self) -> PvForecastResult | None:
        """Current forecast result — from bound strategy."""
        return self.strategy.result

    @classmethod
    def today(cls) -> tuple[PvForecast, ...]:
        """Today-axis variants (AT_6 + LIVE + 4× EXTRAP)."""
        return tuple(v for v in cls if v.is_today)

    @classmethod
    def tomorrow(cls) -> tuple[PvForecast, ...]:
        """Tomorrow-axis variants (TOMORROW_AT_6 + TOMORROW_LIVE)."""
        return tuple(v for v in cls if v.is_tomorrow)

    @classmethod
    def extrap(cls) -> tuple[PvForecast, ...]:
        """EXTRAP variants (4× extrapolated-from-LIVE)."""
        return tuple(v for v in cls if v.is_extrap)


# --- Shared module-level helpers (used by both At6Strategy + LiveStrategy) --- #


def _classify_condition(condition: str) -> str:
    """Classify weather condition into: sunny, partly-variable, partly, cloudy."""
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
