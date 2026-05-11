"""Weather-adjusted PV forecast logic.

Read top-down:
  1. Constants — domain rules (battery capacity, MIN_SOC, modifiers, conditions)
  2. Value objects — vocabulary (SolcastPeriod, AdjustedPeriod, AdjustedPvForecast,
     WeatherConditionAtHour, ConsumptionProfile, TargetSocBucket, TargetSocResult)
  3. PvForecast aggregate — public API head (state + behavior + per-class helpers
     as @staticmethod, see Reguła 2b — pure helpers in stateful class)
  4. Standalone domain utilities — multi-class users (merge_weather_conditions,
     walk_back_workdays) used by application service / infrastructure loader
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Final

# --- Constants --- #

CONSUMPTION_PER_30MIN: Final[float] = 0.45  # kWh (= 0.9 kWh/h / 2)
BATTERY_CAPACITY_KWH: Final[float] = 10.7
MIN_SOC_PERCENT: Final[int] = 10
LOSS_FACTOR: Final[float] = 0.10  # 10% conversion losses
BUFFER_PERCENT: Final[int] = 12
CLOUDY_CAP_HOUR_7: Final[float] = 0.20  # max hourly rate at hour 7 for cloudy

# Prev-workday consumption profile instrumentation (Etap A).
# How many days back we look to build a consumption profile baseline for
# target SOC. Domain decision (not infrastructure detail) — semantics
# "take the last 3 workdays".
PREV_DAYS_COUNT: Final[int] = 3

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


@dataclass(frozen=True)
class ConsumptionProfile:
    """Consumption per 30-min bucket, keyed by (hour, minute) -> kWh."""

    buckets: dict[tuple[int, int], float]
    source_date: date | None = None  # workday the profile was taken from

    def get(self, hour: int, minute: int) -> float | None:
        return self.buckets.get((hour, minute))


@dataclass(frozen=True)
class TargetSocBucket:
    """Per 30-min bucket trace entry used to verify target SOC calculation."""

    period: str  # "HH:MM" local
    pv_kwh: float
    cons_kwh: float
    balance: float
    cumulative: float
    is_min: bool  # True for bucket where cumulative is most negative


@dataclass(frozen=True)
class TargetSocResult:
    """Target SOC + per-bucket trace for observability."""

    value: int  # target SOC percent (MIN_SOC_PERCENT or higher)
    buckets: list[TargetSocBucket]


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
    - consumption_profiles: prev-workday consumption baselines (PREV_DAYS_COUNT)
    - target_soc_*_prev_days: per-prev-workday target SOC (parallel to profiles)
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
    consumption_profiles: list[ConsumptionProfile | None] = field(
        default_factory=lambda: [None] * PREV_DAYS_COUNT
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
    # Extrapolated live variants — recomputed every minute. Three strategies
    # for the in-progress 30-min bucket + future projection (see
    # domain/pv_forecast_extrapolation.py):
    #   - extrapolated_live         : realized prorate (utility meter so-far)
    #   - extrapolated_live_5min    : 5-min average power sensors
    #   - extrapolated_live_pattern : weighted realization factor → projected
    #                                 onto future buckets via [p10, estimate]
    #   - extrapolated_live_proportional : proportional-to-median scoring
    #                                 (S = (real-est)/est, no p10/p90 dependence)
    # Each ExtrapolatedLive bundles adjusted (chart), remaining_kwh (state), and
    # target_soc (SOC% for 7-13 deficit). All variants use CONSUMPTION_PER_30MIN
    # constant baseline for consumption (matching target_soc_live).
    extrapolated_live: ExtrapolatedLive = field(default_factory=ExtrapolatedLive.empty)
    extrapolated_live_5min: ExtrapolatedLive = field(
        default_factory=ExtrapolatedLive.empty
    )
    extrapolated_live_pattern: ExtrapolatedLive = field(
        default_factory=ExtrapolatedLive.empty
    )
    extrapolated_live_proportional: ExtrapolatedLive = field(
        default_factory=ExtrapolatedLive.empty
    )
    # 2-zone band-clamped scoring: S in [-1, +1] anchored at [p10, p90],
    # clamp above p90. See domain/pv_forecast_extrapolation.py.
    extrapolated_live_band: ExtrapolatedLive = field(
        default_factory=ExtrapolatedLive.empty
    )
    # Same band scoring but narrow lookback (current + 1 bucket back only).
    # Captures short-horizon weather trend without morning bias.
    extrapolated_live_band_recent: ExtrapolatedLive = field(
        default_factory=ExtrapolatedLive.empty
    )
    # Hour (0..23) marking the boundary between pre-charge and post-charge in
    # today's 7-13 window. Read from `input_datetime.rce_start_charge_hour_today_override`.
    # Used by _calculate_target_soc to clamp inter-hour surplus during
    # pre-charge (battery doesn't charge from PV → surplus exported, not stored).
    # None = no gate (legacy behavior; accumulate freely).
    start_charge_hour_today: int | None = None

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

    def update_consumption_profiles(
        self, profiles: list[ConsumptionProfile | None], now: datetime
    ) -> None:
        """Refresh prev-workday consumption baselines + downstream target SOC."""
        self.consumption_profiles = profiles
        self._recalculate_target_soc(now)

    def _recalculate_target_soc(self, now: datetime) -> None:
        """Calculate target SOC from current adjusted_* + consumption_profiles.

        Today variants apply the pre-charge inter-hour clamp via
        `start_charge_hour_today` (set by application service before recalc).
        Tomorrow variants currently skip the gate — they'd need a separate
        `start_charge_hour_tomorrow` source which we don't compute yet.
        """
        sch = self.start_charge_hour_today
        if self.adjusted_at_6:
            self.target_soc = self._calculate_target_soc(
                self.adjusted_at_6, now=now, start_charge_hour=sch
            )
        if self.adjusted_live:
            self.target_soc_live = self._calculate_target_soc(
                self.adjusted_live, now=now, start_charge_hour=sch
            )

        # Tomorrow: always full 7-13 window (no `now` arg → simulates entire window).
        # No gate applied — we don't have a tomorrow-specific start_charge_hour yet.
        if self.adjusted_tomorrow:
            self.target_soc_tomorrow = self._calculate_target_soc(
                self.adjusted_tomorrow
            )
        if self.adjusted_tomorrow_live:
            self.target_soc_tomorrow_live = self._calculate_target_soc(
                self.adjusted_tomorrow_live
            )

        # Prev-workday instrumentation (Etap A) — adjusted_live + per-day profile.
        for i, profile in enumerate(self.consumption_profiles):
            if self.adjusted_live and profile is not None:
                self.target_soc_prev_days[i] = self._calculate_target_soc(
                    self.adjusted_live,
                    consumption_profile=profile,
                    now=now,
                    start_charge_hour=sch,
                )
            else:
                self.target_soc_prev_days[i] = None
            if self.adjusted_tomorrow_live and profile is not None:
                self.target_soc_tomorrow_prev_days[i] = self._calculate_target_soc(
                    self.adjusted_tomorrow_live, consumption_profile=profile
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
    def _calculate_target_soc(
        forecast: AdjustedPvForecast,
        consumption_profile: ConsumptionProfile | None = None,
        now: datetime | None = None,
        current_bucket_override: tuple[float, float] | None = None,
        start_charge_hour: int | None = None,
    ) -> TargetSocResult:
        """Calculate target battery SOC + per-bucket trace.

        Simulates cumulative energy deficit from now (or 7:00) to 13:00.
        Before 7:00 or no now: simulates full 7:00-13:00 window.
        After 7:00: simulates from current 30min period to 13:00.
        consumption_profile: per-bucket overrides; fallback to CONSUMPTION_PER_30MIN.

        current_bucket_override=(pv_kwh, cons_kwh): replace the in-progress
        bucket's PV + consumption kWh values (used by extrapolated variants;
        the kWh values represent "remaining contribution in the bucket from
        now onwards").

        start_charge_hour (int | None): pre-charge gate. When set, surplus
        accumulated during pre-charge hours (hour < start_charge_hour) does
        not carry over to the next hour. Battery doesn't charge from PV in
        pre-charge (battery_charge_max_current_toggle=False) — hourly surplus
        is exported, not stored. At each hour boundary where the prior hour
        was pre-charge, cumulative_balance is clamped to ≤ 0 (deficit kept,
        surplus zeroed). See context/target_soc_algorithm.md option A.

        Returns TargetSocResult with .value (SOC percent) and .buckets (trace).
        """
        # Determine start: current 30min period or 7:00
        start_hour = 7
        start_minute = 0
        if now and now.hour >= 7:
            start_hour = now.hour
            start_minute = 0 if now.minute < 30 else 30

        buckets: list[TargetSocBucket] = []
        cumulative_balance = 0.0
        min_balance = 0.0
        min_idx = -1
        prev_hour: int | None = None

        for period in forecast.forecast:
            dt = datetime.fromisoformat(period.period_start)
            hour = dt.hour
            minute = dt.minute
            if hour < start_hour or (hour == start_hour and minute < start_minute):
                continue
            if hour >= 13:
                continue

            # Hour-boundary clamp: if prior hour was in pre-charge, its surplus
            # was exported (not stored in battery) — zero out positive cumulative.
            if (
                prev_hour is not None
                and hour != prev_hour
                and start_charge_hour is not None
                and prev_hour < start_charge_hour
            ):
                cumulative_balance = min(cumulative_balance, 0.0)

            is_current = hour == start_hour and minute == start_minute
            if is_current and current_bucket_override is not None:
                pv_kwh_30min, consumption = current_bucket_override
            else:
                pv_kwh_30min = period.pv_estimate_adjusted / 2  # rate -> kWh per 30min
                consumption = (
                    consumption_profile.get(hour, minute)
                    if consumption_profile
                    else None
                )
                if consumption is None:
                    consumption = CONSUMPTION_PER_30MIN
            balance = pv_kwh_30min - consumption
            cumulative_balance += balance
            if cumulative_balance < min_balance:
                min_balance = cumulative_balance
                min_idx = len(buckets)
            buckets.append(
                TargetSocBucket(
                    period=f"{hour:02d}:{minute:02d}",
                    pv_kwh=round(pv_kwh_30min, 3),
                    cons_kwh=round(consumption, 3),
                    balance=round(balance, 3),
                    cumulative=round(cumulative_balance, 3),
                    is_min=False,  # set below
                )
            )
            prev_hour = hour

        if min_idx >= 0:
            # Replace min bucket with is_min=True (dataclass is frozen → rebuild)
            m = buckets[min_idx]
            buckets[min_idx] = TargetSocBucket(
                period=m.period,
                pv_kwh=m.pv_kwh,
                cons_kwh=m.cons_kwh,
                balance=m.balance,
                cumulative=m.cumulative,
                is_min=True,
            )

        if min_balance >= 0:
            return TargetSocResult(value=MIN_SOC_PERCENT, buckets=buckets)

        deficit_kwh = abs(min_balance)
        deficit_percent = deficit_kwh / (BATTERY_CAPACITY_KWH / 100)
        target = MIN_SOC_PERCENT + deficit_percent * (1 + LOSS_FACTOR) + BUFFER_PERCENT

        return TargetSocResult(
            value=max(round(target), MIN_SOC_PERCENT), buckets=buckets
        )

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


def walk_back_workdays(today: date, days_back: int) -> date | None:
    """Return date N workdays ago (skip weekends).

    Used by infrastructure/pv_forecast/consumption_profile_loader.py to iterate
    over PREV_DAYS_COUNT prev workdays. Pure domain (semantics "skip weekends")
    — does not leak into infrastructure.

    TODO Etap E: replace heuristic with binary_sensor.workday_sensor (PL holidays).
    """
    target = today
    found = 0
    while found < days_back:
        target -= timedelta(days=1)
        if target.weekday() < 5:
            found += 1
        if (today - target).days > 14:  # safety break
            return None
    return target
