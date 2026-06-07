"""Forecast strategy framework + shared value objects.

DFS class ordering (caller zaraz przed callee, depth-first per child):
ForecastStrategy → ForecastContext → ctx VOs (signals, weather/conditions,
solcast) → PvForecastResult → AdjustedPeriod. Concrete strategies live in
sibling modules (`strategies_weather.py`, `strategies_extrapolation.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from ..bucket import Bucket, Buckets
from ..target_soc import PvProfile

# --- Framework: ForecastStrategy (template method) --- #


class ForecastStrategy:
    """Template-method base. Subclasses override `_compute(ctx)`.

    `update()` caches the result, optionally re-patches in-progress
    bucket with live signals (today-variants only), and derives
    `remaining_kwh`. Every strategy exposes the unifying contract
    `(result, total_kwh, remaining_kwh)`.

    Axis flags (`is_today`, `is_extrap`) live on strategy instances —
    `PvForecast` enum delegates `.is_today` / `.is_extrap` properties
    to its bound strategy (no duplication in enum tuple values).
    Defaults: today (`is_today=True`) + non-extrap (`is_extrap=False`).
    At6/LiveStrategy override `is_today` per `today` ctor arg;
    `_ExtrapStrategyBase` overrides `is_extrap=True`.
    """

    supports_in_progress_patch: bool = False
    is_today: bool = True
    is_extrap: bool = False
    pretty_label: str = ""  # display name; concrete strategies must override

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


# --- ForecastContext: input VO for ForecastStrategy.update --- #


@dataclass(frozen=True)
class ForecastContext:
    """Inputs available to strategy updates per dispatch."""

    now: datetime
    signals: LivePvSignals
    weather: WeatherConditions = field(
        default_factory=lambda: WeatherConditions.empty()
    )
    solcast_at_6: list[SolcastPeriod] = field(default_factory=list)
    solcast_today: list[SolcastPeriod] = field(default_factory=list)
    solcast_tomorrow: list[SolcastPeriod] = field(default_factory=list)
    realized_pv_today: dict[tuple[int, int], float] = field(default_factory=dict)
    consumption_w: float | None = None
    start_charge_hour: int | None = None


# --- LivePvSignals: ctx.signals field 1 --- #


@dataclass(frozen=True)
class LivePvSignals:
    """PV-side live readings snapshot — single VO passed to forecasts per tick.

    Replaces 4 separate field writes on the aggregate from application
    service. Service builds via `LiveRateReader` once per tick, hands to
    `forecasts.live_pv_updated(signals, ...)`.
    """

    pv_power_w: float | None = None
    bucket_so_far_kwh: float | None = None
    derivative_w_per_min: float | None = None
    stability_stable: bool | None = None


# --- WeatherConditions: ctx.weather field 2 (merged history + forecast) --- #


@dataclass(frozen=True)
class WeatherConditions:
    """Merged weather snapshot (history + forecast).

    Conditions with no `forecast_date` are ignored — they can't be matched
    to a specific day. `for_hour(...)` accepts a target date for precise
    matching; falls back to undated entries (legacy / single-day callers)
    before defaulting to `"cloudy"`.
    """

    conditions: list[WeatherConditionAtHour]

    @classmethod
    def empty(cls) -> WeatherConditions:
        return cls(conditions=[])

    @classmethod
    def from_history_and_forecast(
        cls,
        history: list[WeatherConditionAtHour],
        forecast: list[WeatherConditionAtHour],
    ) -> WeatherConditions:
        """Merge — forecast wins over history per (date, hour).

        Used by application service (`PvForecastService._build_weather`)
        to assemble the full conditions window for AT6 + LIVE strategies.
        """
        combined: dict[tuple[date, int], WeatherConditionAtHour] = {}
        for c in history:
            if c.forecast_date:
                combined[(c.forecast_date, c.hour)] = c
        for c in forecast:
            if c.forecast_date:
                combined[(c.forecast_date, c.hour)] = c
        return cls(conditions=list(combined.values()))

    def for_hour(self, hour: int, target_date: date | None = None) -> str:
        """Find weather condition for given hour/date. Fallback to 'cloudy'."""
        if target_date:
            for w in self.conditions:
                if w.forecast_date == target_date and w.hour == hour:
                    return w.condition_custom
        for w in self.conditions:
            if w.hour == hour and w.forecast_date is None:
                return w.condition_custom
        return "cloudy"

    def __bool__(self) -> bool:
        return bool(self.conditions)


# --- WeatherConditionAtHour: WeatherConditions.conditions list element --- #


@dataclass
class WeatherConditionAtHour:
    hour: int  # 0-23 local time
    condition_custom: str
    forecast_date: date | None = None  # None = match only by hour


# --- SolcastPeriod: ctx.solcast_* field 3 element --- #


@dataclass
class SolcastPeriod:
    period_start: str  # ISO 8601
    pv_estimate: float  # hourly rate kWh/h
    pv_estimate10: float
    pv_estimate90: float


# --- PvForecastResult: return type from ForecastStrategy._compute --- #


@dataclass
class PvForecastResult:
    forecast: list[AdjustedPeriod]
    total_kwh: float  # sum of (adjusted rate / 2) = actual kWh

    def to_profile(
        self,
        target_date: date | None = None,
        now: datetime | None = None,
        pv_power_w_5min: float | None = None,
    ) -> PvProfile:
        """Project periods → 12-bucket `PvProfile` for 7:00..12:30.

        `pv_estimate_adjusted` is an hourly rate (kWh/h); the profile
        stores kWh per 30-min bucket → divide by 2.

        `target_date`: filter periods to this date. When None, the date of
        the first period is used (single-day forecasts). Missing buckets
        are filled with 0.0 — no PV produced in that slot.

        `now`: when given, the resulting profile holds kWh contributing to
        the deficit calculation FROM NOW ONWARDS. Closed buckets
        (bucket_end <= now) become 0.0; the in-progress bucket
        (bucket_start <= now < bucket_end) is overridden with live
        remaining-kWh derived from `pv_power_w_5min`; future buckets keep
        their full forecast kWh. When `now` is None the profile is a
        plain forecast snapshot (back-compat for tomorrow / matrix
        non-today).

        `pv_power_w_5min`: instantaneous PV power, typically the 5-min
        average from `sensor.pv_power_avg_5_minutes`. Integrated over the
        remaining seconds in the in-progress bucket to produce live
        kWh-remaining. Fail-hard contract: required when `now` is given
        (raises ValueError if None). Caller skips the recalc or passes
        `now=None` if live data is not yet available. The integration
        formula will later grow derivative-aware projection (stable rise
        on clear-sky mornings) — kept on the VO method so consumption's
        simpler integration in `ConsumptionProfile.to_view` is unaffected.

        Raises `ValueError` when no period matches `target_date` (the
        caller is asking for a day the forecast doesn't cover, e.g.
        the matrix date-picker pointing at day-after-tomorrow with only
        today/tomorrow forecast available), OR when `now` is given but
        `pv_power_w_5min` is None.
        """
        if now is not None and pv_power_w_5min is None:
            raise ValueError(
                "PvForecastResult.to_profile: pv_power_w_5min is required "
                "when `now` is given"
            )
        inferred: date | None = None
        by_bucket: dict[Bucket, float] = {}
        matched = False
        for period in self.forecast:
            dt = datetime.fromisoformat(period.period_start)
            if target_date is None and inferred is None:
                inferred = dt.date()
            match_date = target_date if target_date is not None else inferred
            if dt.date() != match_date:
                continue
            matched = True
            if dt.hour < 7 or dt.hour >= 13 or dt.minute not in (0, 30):
                continue
            by_bucket[Bucket(dt.hour, dt.minute)] = round(
                period.pv_estimate_adjusted / 2, 4
            )
        if not matched:
            raise ValueError(
                f"PvForecastResult.to_profile: no periods match {target_date!r}"
            )
        for h in range(7, 13):
            for m in (0, 30):
                by_bucket.setdefault(Bucket(h, m), 0.0)
        buckets = Buckets(by_bucket=by_bucket)
        if now is not None:
            assert pv_power_w_5min is not None  # narrowed by guard above
            buckets = buckets.from_now(
                now, Bucket.live_remaining_kwh(now, pv_power_w_5min)
            )
        return PvProfile(buckets=buckets)

    def with_now_aware_in_progress(
        self,
        now: datetime,
        pv_power_w_5min: float,
        pv_bucket_so_far_kwh: float,
    ) -> PvForecastResult:
        """Return a copy with in-progress period rescaled to full-bucket estimate.

        Rate = `(so_far + live_remaining_kwh) × 2` (kWh/h). Other periods
        unchanged.

        Used for chart display so the in-progress dot reflects the same bucket
        value the strategy `score` and `target_soc` paths use internally — the
        single source of truth is `Bucket.full_bucket_kwh`.

        Caller must guarantee `now` falls in a 30-min slot covered by the
        forecast (typically a today period in the 7-13 window). If the
        in-progress slot isn't in `self.forecast`, the method is a no-op
        (returns a structural copy with the same values).
        """
        rate = Bucket.full_bucket_kwh(now, pv_power_w_5min, pv_bucket_so_far_kwh) * 2.0
        return self._rebuild(now, current_rate=rate, future_overrides=None)

    def with_now_aware_in_progress_and_future_overrides(
        self,
        now: datetime,
        pv_power_w_5min: float,
        pv_bucket_so_far_kwh: float,
        future_pv_kwh_per_h_overrides: dict[tuple[int, int], float],
    ) -> PvForecastResult:
        """As `with_now_aware_in_progress`, plus future-bucket overrides.

        Future periods (bucket_start > now) get `pv_estimate_adjusted` replaced
        by their corresponding entry in `future_pv_kwh_per_h_overrides`
        (kWh/h rate keyed by `(hour, minute)`). Periods without an override
        entry keep their original forecast value. Used by strategy variants
        in `strategies_extrapolation` whose projection produces per-bucket
        future rates.
        """
        rate = Bucket.full_bucket_kwh(now, pv_power_w_5min, pv_bucket_so_far_kwh) * 2.0
        return self._rebuild(
            now, current_rate=rate, future_overrides=future_pv_kwh_per_h_overrides
        )

    def _rebuild(
        self,
        now: datetime,
        *,
        current_rate: float,
        future_overrides: dict[tuple[int, int], float] | None,
    ) -> PvForecastResult:
        """Build a new PvForecastResult with selective period replacement.

        - in-progress period (Bucket.enclosing(now)) → `current_rate`
        - future periods (bucket_start > now): take `future_overrides[(h,m)]`
          when present, else keep `pv_estimate_adjusted` unchanged
        - past periods: unchanged
        """
        current_bucket = Bucket.enclosing(now)
        new_periods: list[AdjustedPeriod] = []
        total_kwh = 0.0
        for period in self.forecast:
            dt = datetime.fromisoformat(period.period_start)
            if dt.date() != now.date():
                # Not today's period (e.g. tomorrow in a multi-day forecast) —
                # never patch, never relevant for the in-progress bucket on `now`.
                rate = period.pv_estimate_adjusted
            else:
                period_bucket = (
                    Bucket(dt.hour, dt.minute) if dt.minute in (0, 30) else None
                )
                if period_bucket == current_bucket:
                    rate = current_rate
                elif (
                    period_bucket is not None
                    and future_overrides is not None
                    and period_bucket.is_future_at(now)
                ):
                    rate = future_overrides.get(
                        (period_bucket.hour, period_bucket.minute),
                        period.pv_estimate_adjusted,
                    )
                else:
                    rate = period.pv_estimate_adjusted
            new_periods.append(
                AdjustedPeriod(
                    period_start=period.period_start,
                    pv_estimate_adjusted=round(rate, 4),
                )
            )
            total_kwh += rate / 2.0
        return PvForecastResult(forecast=new_periods, total_kwh=round(total_kwh, 4))

    def remaining_kwh_from(self, now: datetime) -> float:
        """Sum kWh from `now`'s bucket onwards (past excluded).

        Returns the full per-bucket value for the bucket containing
        `now` (no proration of in-progress) — matches the semantics of
        `_assemble` in `extrapolation_utils` which rescales
        in-progress before summing. Tomorrow-axis callers pass `now`
        from today; the comparison naturally includes all of tomorrow's
        periods.
        """
        start_hour = now.hour
        start_minute = 0 if now.minute < 30 else 30
        total = 0.0
        for period in self.forecast:
            dt = datetime.fromisoformat(period.period_start)
            # Filter by date as well so tomorrow's periods don't get filtered
            # out by today's start_hour (they're on a later date).
            if dt.date() < now.date():
                continue
            if dt.date() == now.date() and (
                dt.hour < start_hour
                or (dt.hour == start_hour and dt.minute < start_minute)
            ):
                continue
            total += period.pv_estimate_adjusted / 2
        return round(total, 4)


# --- AdjustedPeriod: PvForecastResult.forecast list element --- #


@dataclass
class AdjustedPeriod:
    period_start: str  # ISO 8601
    pv_estimate_adjusted: float  # hourly rate kWh/h
