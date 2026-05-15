"""Weather-adjusted PV forecast logic.

Read top-down:
  1. Constants — domain rules (battery capacity, MIN_SOC, modifiers, conditions)
  2. Value objects — vocabulary (SolcastPeriod, AdjustedPeriod, AdjustedPvForecast,
     WeatherConditionAtHour, ConsumptionProfile, TargetSocBucket, TargetSocResult)
  3. PvForecast aggregate — public API head (state + behavior + per-class helpers
     as @staticmethod, see Reguła 2b — pure helpers in stateful class)
  4. Standalone domain utilities — multi-class users (merge_weather_conditions,
     walk_back_workdays) used by application service / infrastructure loader

Target SOC formula + its constants + result dataclasses live in
`domain/target_soc.py` — re-exported here for back-compat (existing
callers in pv_forecast_extrapolation.py and tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Final

from .bucket_math import (
    Bucket,
    buckets_from_now,
    full_bucket_kwh as bucket_full_kwh,
    live_remaining_kwh,
)
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
class AdjustedPvForecast:
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
                "AdjustedPvForecast.to_profile: pv_power_w_5min is required "
                "when `now` is given"
            )
        inferred: date | None = None
        buckets: dict[tuple[int, int], float] = {}
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
            buckets[(dt.hour, dt.minute)] = round(period.pv_estimate_adjusted / 2, 4)
        if not matched:
            raise ValueError(
                f"AdjustedPvForecast.to_profile: no periods match {target_date!r}"
            )
        for h in range(7, 13):
            for m in (0, 30):
                buckets.setdefault((h, m), 0.0)
        if now is not None:
            assert pv_power_w_5min is not None  # narrowed by guard above
            buckets = buckets_from_now(
                buckets,
                now=now,
                live_remaining_kwh=live_remaining_kwh(now, pv_power_w_5min),
            )
        return PvProfile(buckets=buckets)

    def with_now_aware_in_progress(
        self,
        now: datetime,
        pv_power_w_5min: float,
        pv_bucket_so_far_kwh: float,
    ) -> AdjustedPvForecast:
        """Return a copy with in-progress period rescaled to full-bucket estimate.

        Rate = `(so_far + live_remaining_kwh) × 2` (kWh/h). Other periods
        unchanged.

        Used for chart display so the in-progress dot reflects the same bucket
        value the strategy `score` and `target_soc` paths use internally — the
        single source of truth is `bucket_math.full_bucket_kwh`.

        Caller must guarantee `now` falls in a 30-min slot covered by the
        forecast (typically a today period in the 7-13 window). If the
        in-progress slot isn't in `self.forecast`, the method is a no-op
        (returns a structural copy with the same values).
        """
        rate = bucket_full_kwh(now, pv_power_w_5min, pv_bucket_so_far_kwh) * 2.0
        return self._rebuild(now, current_rate=rate, future_overrides=None)

    def with_now_aware_in_progress_and_future_overrides(
        self,
        now: datetime,
        pv_power_w_5min: float,
        pv_bucket_so_far_kwh: float,
        future_pv_kwh_per_h_overrides: dict[tuple[int, int], float],
    ) -> AdjustedPvForecast:
        """As `with_now_aware_in_progress`, plus future-bucket overrides.

        Future periods (bucket_start > now) get `pv_estimate_adjusted` replaced
        by their corresponding entry in `future_pv_kwh_per_h_overrides`
        (kWh/h rate keyed by `(hour, minute)`). Periods without an override
        entry keep their original forecast value. Used by strategy variants
        in `pv_forecast_extrapolation` whose projection produces per-bucket
        future rates.
        """
        rate = bucket_full_kwh(now, pv_power_w_5min, pv_bucket_so_far_kwh) * 2.0
        return self._rebuild(
            now, current_rate=rate, future_overrides=future_pv_kwh_per_h_overrides
        )

    def _rebuild(
        self,
        now: datetime,
        *,
        current_rate: float,
        future_overrides: dict[tuple[int, int], float] | None,
    ) -> AdjustedPvForecast:
        """Build a new AdjustedPvForecast with selective period replacement.

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
        return AdjustedPvForecast(forecast=new_periods, total_kwh=round(total_kwh, 4))


@dataclass(frozen=True)
class ExtrapolatedLive:
    """One extrapolation strategy applied to today's live forecast.

    Bundle of three correlated outputs computed from the same in-progress
    bucket assumption (see pv_forecast_extrapolation.py for strategies):
    - adjusted: full per-period AdjustedPvForecast (chart attribute)
    - remaining_kwh: scalar kWh remaining today, past excluded (sensor state)
    - target_soc: SOC % needed for 7-13 deficit window (sensor value)

    Any field can be None when source data is unavailable; ExtrapolatedLive.empty()
    represents "no data — sensor → unknown".
    """

    adjusted: AdjustedPvForecast | None
    remaining_kwh: float | None
    target_soc: TargetSocResult | None

    @classmethod
    def empty(cls) -> ExtrapolatedLive:
        return cls(adjusted=None, remaining_kwh=None, target_soc=None)


# --- Aggregate (rich domain model) --- #


@dataclass
class PvForecast:
    """Aggregate holding current weather-adjusted PV estimates + target SoC.

    State + behavior together (rich domain model). Update methods take value
    objects (already built by application service from driving adapters) —
    domain knows nothing about data sources (HA states), only their semantics.

    8 forecast/SoC fields + prev-workday matrix (Etap A instrumentation):
    - adjusted_*: weather-adjusted PV estimates (today/tomorrow × at_6/live)
    - target_soc_*: implied battery SOC target (today/tomorrow × at_6/live)
    - consumption_profiles: rich `ConsumptionProfiles` entity holding two
      anchor sets (today + tomorrow), each PREV_DAYS_COUNT long. Knows
      how to refresh itself + retry on partial fetch.
    - target_soc_*_prev_days: per-prev-workday target SOC (parallel to
      `consumption_profiles.today_profiles` /
      `consumption_profiles.tomorrow_profiles` respectively)
    - target_soc_max / target_soc_tomorrow_max: max(live + prev_days) — final
      decision input for automations.
    """

    adjusted_at_6: AdjustedPvForecast | None = None
    adjusted_live: AdjustedPvForecast | None = None
    adjusted_tomorrow: AdjustedPvForecast | None = None
    adjusted_tomorrow_live: AdjustedPvForecast | None = None
    target_soc: TargetSocResult | None = None
    target_soc_live: TargetSocResult | None = None
    target_soc_tomorrow: TargetSocResult | None = None
    target_soc_tomorrow_live: TargetSocResult | None = None
    consumption_profiles: ConsumptionProfiles = field(
        default_factory=lambda: ConsumptionProfiles.empty()
    )
    target_soc_prev_days: list[TargetSocResult | None] = field(
        default_factory=lambda: [None] * PREV_DAYS_COUNT
    )
    target_soc_tomorrow_prev_days: list[TargetSocResult | None] = field(
        default_factory=lambda: [None] * PREV_DAYS_COUNT
    )
    target_soc_max: int | None = None
    target_soc_tomorrow_max: int | None = None
    # Raw Solcast live periods (preserved alongside adjusted_live) — needed by
    # the calibrated_pattern extrapolation variant which uses pv_estimate +
    # pv_estimate10 (raw range, not weather-adjusted) as the projection axis.
    solcast_live: list[SolcastPeriod] = field(default_factory=list)
    # Extrapolated live variants — recomputed every minute. Four future-bucket
    # projection strategies (see domain/pv_forecast_extrapolation.py):
    #   - extrapolated_live_pattern : weighted realization factor → projected
    #                                 onto future buckets via [p10, est, p90]
    #   - extrapolated_live_proportional : proportional-to-median scoring
    #                                 (S = (real-est)/est, no p10/p90 dependence)
    #   - extrapolated_live_band         : 2-zone score [p10, p90] (no est)
    #   - extrapolated_live_band_recent  : band scoring, narrow lookback
    # Each bundles adjusted (chart), remaining_kwh (state), and
    # target_soc (SOC% for 7-13 deficit). All variants share the
    # uniform in-progress bucket treatment baked into to_profile() —
    # live pv_power_w_5min over remaining seconds — so they differ
    # ONLY in their future bucket projection. Consumption uses the
    # flat baseline shifted by live consumption.
    extrapolated_live_pattern: ExtrapolatedLive = field(
        default_factory=ExtrapolatedLive.empty
    )
    extrapolated_live_proportional: ExtrapolatedLive = field(
        default_factory=ExtrapolatedLive.empty
    )
    extrapolated_live_band: ExtrapolatedLive = field(
        default_factory=ExtrapolatedLive.empty
    )
    extrapolated_live_band_recent: ExtrapolatedLive = field(
        default_factory=ExtrapolatedLive.empty
    )
    # Hour (0..23) marking the boundary between pre-charge and post-charge in
    # today's 7-13 window. Read from `input_datetime.rce_start_charge_hour_today_override`.
    # Used by calculate_target_soc to clamp inter-hour surplus during
    # pre-charge (battery doesn't charge from PV → surplus exported, not stored).
    # None = no gate (legacy behavior; accumulate freely).
    start_charge_hour_today: int | None = None
    # Same gate for tomorrow — read from `sensor.rce_start_charge_hour_tomorrow_time`.
    # No user-facing override sensor yet; can be swapped later. Applied to
    # `target_soc_tomorrow*` so surplus from a sunny pre-charge hour doesn't
    # mask a real deficit later in the window.
    start_charge_hour_tomorrow: int | None = None
    # Live house consumption rate (W) — sourced from
    # `sensor.house_consumption_minus_water_avg_5_minutes` via
    # `LiveRateReader.read_consumption_w()`. Integrated by
    # `ConsumptionProfile.to_view` over the remaining seconds in the
    # in-progress bucket to produce live kWh-remaining for today's
    # target_soc variants.
    live_consumption_w: float | None = None
    # Live PV generation rate (W) — sourced from
    # `sensor.pv_power_avg_5_minutes` via `LiveRateReader.read_pv_power_w()`.
    # Symmetric counterpart of `live_consumption_w`: integrated by
    # `AdjustedPvForecast.to_profile` for the in-progress bucket of
    # today's PV-side variants.
    live_pv_power_w: float | None = None

    def update_at_6(
        self,
        solcast_periods: list[SolcastPeriod],
        weather_conditions: list[WeatherConditionAtHour],
        now: datetime,
    ) -> None:
        """Update morning (AT6) snapshot — adjusted_at_6 + downstream target SOC."""
        self.adjusted_at_6 = self._adjust_pv_forecast_at6(
            solcast_periods, weather_conditions
        )
        self._recalculate_target_soc(now)

    def update_live(
        self,
        solcast_periods: list[SolcastPeriod],
        weather_conditions: list[WeatherConditionAtHour],
        now: datetime,
    ) -> None:
        """Update live forecast — adjusted_live + raw solcast_live + downstream target SOC."""
        self.solcast_live = solcast_periods
        self.adjusted_live = self._adjust_pv_forecast_live(
            solcast_periods, weather_conditions, now
        )
        self._recalculate_target_soc(now)

    def update_tomorrow(
        self,
        solcast_periods: list[SolcastPeriod],
        weather_conditions: list[WeatherConditionAtHour],
        now: datetime,
    ) -> None:
        """Update tomorrow snapshots — adjusted_tomorrow (AT6 mods) + adjusted_tomorrow_live (LIVE mods).

        Two variants with DIFFERENT adjustment semantics:
        - adjusted_tomorrow      — AT6 modifiers (pessimistic, cloudy cap @ hour 7).
                                   Evening planning: safety lower-bound.
        - adjusted_tomorrow_live — LIVE modifiers (optimistic, no cap).
                                   After midnight rollover: aligns with target_soc_live
                                   for continuity (yesterday's target_soc_tomorrow_live
                                   ~ today's target_soc_live).
        """
        self.adjusted_tomorrow = self._adjust_pv_forecast_at6(
            solcast_periods, weather_conditions
        )
        # _adjust_pv_forecast_live checks is_first_hour = (period.hour == now.hour).
        # For tomorrow's periods (date = tomorrow) no match → all periods use
        # standard LIVE modifiers (no special first-hour treatment).
        self.adjusted_tomorrow_live = self._adjust_pv_forecast_live(
            solcast_periods, weather_conditions, now
        )
        self._recalculate_target_soc(now)

    def recalculate_target_soc(self, now: datetime) -> None:
        """Public hook used by `ConsumptionProfiles.refresh_*` callers.

        The entity mutates `consumption_profiles.today_profiles` /
        `tomorrow_profiles` in place; the aggregate then refreshes its
        downstream `target_soc_*` cache via this public method (private
        `_recalculate_target_soc` remains for internal use).
        """
        self._recalculate_target_soc(now)

    def _recalculate_target_soc(self, now: datetime) -> None:
        """Calculate target SOC from current adjusted_* + consumption_profiles.

        Today variants build now-aware profiles via
        `AdjustedPvForecast.to_profile(today, now, pv_power_w_5min=...)`
        and `ConsumptionProfile.to_view(now, live_consumption_w=...)`.
        When either live signal is missing, today variants stay `None`
        (fail-hard contract — no stale forecast-prorate fallback).

        Tomorrow variants pass `now=None` (full-window deficit, no live
        in-progress concept since current power doesn't carry across
        days), so live signals are not needed.

        Pre-charge inter-hour clamp via `start_charge_hour_{today,tomorrow}`
        applies symmetrically: a sunny pre-charge hour cannot mask a later
        deficit by propagating its positive cumulative balance across the
        hour boundary into the gated post-charge window.
        """
        sch = self.start_charge_hour_today
        sch_t = self.start_charge_hour_tomorrow
        live_cons_w = self.live_consumption_w
        live_pv_w = self.live_pv_power_w
        default_cons = ConsumptionProfile.flat()
        today = now.date()
        tomorrow = today + timedelta(days=1)

        # Today block — needs both live signals or sets to None
        today_ready = live_cons_w is not None and live_pv_w is not None
        if today_ready:
            cons_view_today = default_cons.to_view(
                now=now, live_consumption_w=live_cons_w
            )
            at6_profile = (
                self.adjusted_at_6.to_profile(today, now=now, pv_power_w_5min=live_pv_w)
                if self.adjusted_at_6
                else None
            )
            live_profile = (
                self.adjusted_live.to_profile(today, now=now, pv_power_w_5min=live_pv_w)
                if self.adjusted_live
                else None
            )
            self.target_soc = (
                calculate_target_soc(
                    at6_profile, cons_view_today, start_charge_hour=sch
                )
                if at6_profile is not None
                else None
            )
            self.target_soc_live = (
                calculate_target_soc(
                    live_profile, cons_view_today, start_charge_hour=sch
                )
                if live_profile is not None
                else None
            )
        else:
            live_profile = None
            self.target_soc = None
            self.target_soc_live = None

        # Tomorrow: full 7-13 window; no live override (current power doesn't
        # carry across days). Plain profile snapshots — `now=None` path.
        tomorrow_live_profile = (
            self.adjusted_tomorrow_live.to_profile(tomorrow)
            if self.adjusted_tomorrow_live
            else None
        )
        if self.adjusted_tomorrow:
            self.target_soc_tomorrow = calculate_target_soc(
                self.adjusted_tomorrow.to_profile(tomorrow),
                default_cons,
                start_charge_hour=sch_t,
            )
        if tomorrow_live_profile is not None:
            self.target_soc_tomorrow_live = calculate_target_soc(
                tomorrow_live_profile, default_cons, start_charge_hour=sch_t
            )

        # Prev-workday instrumentation. Two anchor sets:
        # - today_profiles: anchored at today → prev_1 = yesterday workday
        # - tomorrow_profiles: anchored at tomorrow → prev_1 = today workday
        # Today-anchored sensors share `live_profile` (now-aware PV) and
        # apply per-prev-profile to_view; tomorrow-anchored sensors use full
        # snapshots (no live override).
        for i, profile in enumerate(self.consumption_profiles.today_profiles):
            if profile is None or live_profile is None or not today_ready:
                self.target_soc_prev_days[i] = None
                continue
            assert live_cons_w is not None  # narrowed by today_ready
            self.target_soc_prev_days[i] = calculate_target_soc(
                live_profile,
                profile.to_view(now=now, live_consumption_w=live_cons_w),
                start_charge_hour=sch,
            )
        for i, profile in enumerate(self.consumption_profiles.tomorrow_profiles):
            if tomorrow_live_profile is not None and profile is not None:
                self.target_soc_tomorrow_prev_days[i] = calculate_target_soc(
                    tomorrow_live_profile,
                    profile,
                    start_charge_hour=sch_t,
                )
            else:
                self.target_soc_tomorrow_prev_days[i] = None

        today_vals = [
            r.value
            for r in [self.target_soc_live, *self.target_soc_prev_days]
            if r is not None
        ]
        self.target_soc_max = max(today_vals) if today_vals else None
        tmrw_vals = [
            r.value
            for r in [
                self.target_soc_tomorrow_live,
                *self.target_soc_tomorrow_prev_days,
            ]
            if r is not None
        ]
        self.target_soc_tomorrow_max = max(tmrw_vals) if tmrw_vals else None

    @staticmethod
    def _adjust_pv_forecast_at6(
        solcast_periods: list[SolcastPeriod],
        weather_conditions: list[WeatherConditionAtHour],
    ) -> AdjustedPvForecast:
        """Adjust morning Solcast forecast (snapshot from 6:05) using weather."""
        forecast = []
        total_kwh = 0.0

        for period in solcast_periods:
            dt = datetime.fromisoformat(period.period_start)
            hour = dt.hour
            target_date = dt.date()
            condition = PvForecast._get_condition_for_hour(
                hour, weather_conditions, target_date
            )
            adj_rate = PvForecast._adjust_at6_period(period, condition, hour)

            forecast.append(
                AdjustedPeriod(
                    period_start=period.period_start,
                    pv_estimate_adjusted=round(adj_rate, 4),
                )
            )
            total_kwh += adj_rate / 2  # rate -> kWh per 30min

        return AdjustedPvForecast(forecast=forecast, total_kwh=round(total_kwh, 4))

    @staticmethod
    def _adjust_at6_period(period: SolcastPeriod, condition: str, hour: int) -> float:
        """Apply AT6 weather adjustment. Returns adjusted hourly rate."""
        cat = PvForecast._classify_condition(condition)

        if cat == "sunny":
            return period.pv_estimate * 1.0
        if cat == "partly-variable":
            return period.pv_estimate * 0.8
        if cat == "partly":
            return period.pv_estimate * 0.7

        # cloudy/other
        if hour <= 10:
            modifier = AT6_CLOUDY_MODIFIER_EARLY
        else:
            modifier = AT6_CLOUDY_MODIFIER_LATE

        adj = period.pv_estimate10 * modifier

        if hour == 7:
            adj = min(adj, CLOUDY_CAP_HOUR_7)

        return adj

    @staticmethod
    def _adjust_pv_forecast_live(
        solcast_periods: list[SolcastPeriod],
        weather_conditions: list[WeatherConditionAtHour],
        now: datetime,
    ) -> AdjustedPvForecast:
        """Adjust live Solcast forecast using weather. First hour treated differently."""
        forecast = []
        total_kwh = 0.0
        current_hour = now.hour

        for period in solcast_periods:
            dt = datetime.fromisoformat(period.period_start)
            hour = dt.hour
            target_date = dt.date()
            is_first_hour = hour == current_hour
            condition = PvForecast._get_condition_for_hour(
                hour, weather_conditions, target_date
            )
            adj_rate = PvForecast._adjust_live_period(period, condition, is_first_hour)

            forecast.append(
                AdjustedPeriod(
                    period_start=period.period_start,
                    pv_estimate_adjusted=round(adj_rate, 4),
                )
            )
            total_kwh += adj_rate / 2

        return AdjustedPvForecast(forecast=forecast, total_kwh=round(total_kwh, 4))

    @staticmethod
    def _adjust_live_period(
        period: SolcastPeriod, condition: str, is_first_hour: bool
    ) -> float:
        """Apply LIVE weather adjustment. Returns adjusted hourly rate."""
        cat = PvForecast._classify_condition(condition)

        if is_first_hour:
            # Trust Solcast for the next hour, only swap est->est10 for cloudy
            if cat == "cloudy":
                return period.pv_estimate10 * 1.0
            return period.pv_estimate * 1.0

        # Remaining hours
        if cat == "sunny":
            return period.pv_estimate * 1.0
        if cat == "partly-variable":
            return period.pv_estimate * 0.8
        if cat == "partly":
            return period.pv_estimate * 0.7

        # cloudy/other — est10 without additional modifier (Solcast live already corrected)
        return period.pv_estimate10 * 1.0

    # --- common helpers (multi-caller — Reguła 2a) --- #

    @staticmethod
    def _get_condition_for_hour(
        hour: int,
        weather_conditions: list[WeatherConditionAtHour],
        target_date: date | None = None,
    ) -> str:
        """Find weather condition for given hour and date. Fallback to cloudy.

        Multi-caller: _adjust_pv_forecast_at6, _adjust_pv_forecast_live.
        """
        # Exact match: date + hour
        if target_date:
            for w in weather_conditions:
                if w.forecast_date == target_date and w.hour == hour:
                    return w.condition_custom
        # Fallback: hour only (for conditions without date)
        for w in weather_conditions:
            if w.hour == hour and w.forecast_date is None:
                return w.condition_custom
        return "cloudy"  # pessimistic fallback

    @staticmethod
    def _classify_condition(condition: str) -> str:
        """Classify condition into: sunny, partly-variable, partly, cloudy.

        Multi-caller: _adjust_at6_period, _adjust_live_period.
        """
        if condition in SUNNY_CONDITIONS:
            return "sunny"
        if condition in PARTLY_VARIABLE_CONDITIONS:
            return "partly-variable"
        if condition in PARTLY_CONDITIONS:
            return "partly"
        return "cloudy"


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
    the full conditions window for `PvForecast._adjust_pv_forecast_*`.
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
