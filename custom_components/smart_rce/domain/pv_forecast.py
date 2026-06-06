"""PV forecast vocabulary — value objects + constants + utilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from .bucket import Bucket, Buckets
from .consumption_profiles import (
    PREV_DAYS_COUNT,
    ConsumptionProfile,
    ConsumptionProfiles,
    ConsumptionProfileSource,
)
from .target_soc import (
    BATTERY_CAPACITY_KWH,
    BUFFER_PERCENT,
    CONSUMPTION_PER_30MIN,
    LOSS_FACTOR,
    MIN_SOC_PERCENT,
    PvProfile,
    TargetSocBucket,
    TargetSocResult,
    calculate_target_soc,
)

__all__ = [
    "BATTERY_CAPACITY_KWH",
    "BUFFER_PERCENT",
    "CONSUMPTION_PER_30MIN",
    "ConsumptionProfile",
    "ConsumptionProfiles",
    "ConsumptionProfileSource",
    "LOSS_FACTOR",
    "MIN_SOC_PERCENT",
    "PREV_DAYS_COUNT",
    "PvProfile",
    "TargetSocBucket",
    "TargetSocResult",
    "calculate_target_soc",
    # plus everything else exported by this module (implicit)
]

# --- Constants --- #

CLOUDY_CAP_HOUR_7: Final[float] = 0.20  # max hourly rate at hour 7 for cloudy

# AT6 cloudy modifiers per hour (hourly rate multiplier on est10)
AT6_CLOUDY_MODIFIER_EARLY: Final[float] = 0.5  # hours 7-10
AT6_CLOUDY_MODIFIER_LATE: Final[float] = 0.7  # hours 11+

# Conditions that count as "cloudy" (everything not explicitly mapped)
SUNNY_CONDITIONS = frozenset({"sunny", "clear-night"})
PARTLY_VARIABLE_CONDITIONS = frozenset({"partlycloudy-variable"})
PARTLY_CONDITIONS = frozenset({"partlycloudy"})
# Everything else = cloudy/other


# --- Value objects --- #


@dataclass
class SolcastPeriod:
    period_start: str  # ISO 8601
    pv_estimate: float  # hourly rate kWh/h
    pv_estimate10: float
    pv_estimate90: float


@dataclass
class AdjustedPeriod:
    period_start: str  # ISO 8601
    pv_estimate_adjusted: float  # hourly rate kWh/h


@dataclass
class WeatherConditionAtHour:
    hour: int  # 0-23 local time
    condition_custom: str
    forecast_date: date | None = None  # None = match only by hour


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
        in `pv_forecast_extrapolation` whose projection produces per-bucket
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
        `_assemble` in `pv_forecast_extrapolation` which rescales
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


# --- LivePvSignals VO --- #


@dataclass(frozen=True)
class LivePvSignals:
    """PV-side live readings snapshot — single VO passed to updater per tick.

    Replaces 4 separate field writes on the aggregate from application
    service. Service builds via `LiveRateReader` once per tick, hands to
    `updater.refresh_live_signals(signals)`.
    """

    pv_power_w: float | None = None
    bucket_so_far_kwh: float | None = None
    derivative_w_per_min: float | None = None
    stability_stable: bool | None = None


# --- Standalone domain utilities (multi-class users) --- #


def merge_weather_conditions(
    history: list[WeatherConditionAtHour],
    forecast: list[WeatherConditionAtHour],
) -> list[WeatherConditionAtHour]:
    """Merge history (past hours, frozen) with forecast (future hours, fresh).

    Forecast wins over history per (date, hour) — more current data for future
    slots. Conditions without forecast_date are ignored (cannot be matched to
    a specific day).

    Used by application service (PvForecastService._build_weather) to assemble
    the full conditions window for `PvForecasts._adjust_pv_forecast_*`.
    """
    combined: dict[tuple[date, int], WeatherConditionAtHour] = {}
    for c in history:
        if c.forecast_date:
            combined[(c.forecast_date, c.hour)] = c
    for c in forecast:
        if c.forecast_date:
            combined[(c.forecast_date, c.hour)] = c
    return list(combined.values())


def walk_back_workdays(
    today: date,
    days_back: int,
    workday_dates: set[date],
) -> date | None:
    """Return the N-th most recent workday strictly before `today`.

    `workday_dates` is the authoritative set of workdays in a sufficiently
    wide lookback window (typically 30 days back) — sourced from the HA
    workday calendar by `WorkdayCalendarReader`. No "skip weekends"
    fallback: if the set is empty (calendar unavailable) or shallower
    than `days_back`, returns None. Callers log a clear warning so the
    missing calendar is visible rather than silently masked by a
    heuristic that ignores holidays.
    """
    if not workday_dates:
        return None
    sorted_back = sorted((d for d in workday_dates if d < today), reverse=True)
    if days_back <= 0 or days_back > len(sorted_back):
        return None
    return sorted_back[days_back - 1]
