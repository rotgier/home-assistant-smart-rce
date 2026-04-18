"""Weather-adjusted PV forecast logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

CONSUMPTION_PER_30MIN: Final[float] = 0.45  # kWh (= 0.9 kWh/h / 2)
BATTERY_CAPACITY_KWH: Final[float] = 10.7
MIN_SOC_PERCENT: Final[int] = 10
LOSS_FACTOR: Final[float] = 0.10  # 10% conversion losses
BUFFER_PERCENT: Final[int] = 12
CLOUDY_CAP_HOUR_7: Final[float] = 0.20  # max hourly rate at hour 7 for cloudy

# AT6 cloudy modifiers per hour (hourly rate multiplier on est10)
AT6_CLOUDY_MODIFIER_EARLY: Final[float] = 0.5  # hours 7-10
AT6_CLOUDY_MODIFIER_LATE: Final[float] = 0.7  # hours 11+

# Conditions that count as "cloudy" (everything not explicitly mapped)
SUNNY_CONDITIONS = frozenset({"sunny", "clear-night"})
PARTLY_VARIABLE_CONDITIONS = frozenset({"partlycloudy-variable"})
PARTLY_CONDITIONS = frozenset({"partlycloudy"})
# Everything else = cloudy/inne


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

    def get(self, hour: int, minute: int) -> float | None:
        return self.buckets.get((hour, minute))


def _get_condition_for_hour(
    hour: int,
    weather_conditions: list[WeatherConditionAtHour],
    target_date: date | None = None,
) -> str:
    """Find weather condition for given hour and date. Fallback to cloudy."""
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


def _classify_condition(condition: str) -> str:
    """Classify condition into: sunny, partly-variable, partly, cloudy."""
    if condition in SUNNY_CONDITIONS:
        return "sunny"
    if condition in PARTLY_VARIABLE_CONDITIONS:
        return "partly-variable"
    if condition in PARTLY_CONDITIONS:
        return "partly"
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

    # cloudy/inne
    if hour <= 10:
        modifier = AT6_CLOUDY_MODIFIER_EARLY
    else:
        modifier = AT6_CLOUDY_MODIFIER_LATE

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

    # cloudy/inne — est10 without additional modifier (Solcast live already corrected)
    return period.pv_estimate10 * 1.0


def adjust_pv_forecast_at6(
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
        condition = _get_condition_for_hour(hour, weather_conditions, target_date)
        adj_rate = _adjust_at6_period(period, condition, hour)

        forecast.append(
            AdjustedPeriod(
                period_start=period.period_start,
                pv_estimate_adjusted=round(adj_rate, 4),
            )
        )
        total_kwh += adj_rate / 2  # rate -> kWh per 30min

    return AdjustedPvForecast(forecast=forecast, total_kwh=round(total_kwh, 4))


def adjust_pv_forecast_live(
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


def calculate_target_soc(
    forecast: AdjustedPvForecast,
    is_workday: bool,
    consumption_profile: ConsumptionProfile | None = None,
    now: datetime | None = None,
) -> int:
    """Calculate target battery SOC based on adjusted PV forecast.

    Simulates cumulative energy deficit from now (or 7:00) to 13:00.
    Before 7:00 or no now: simulates full 7:00-13:00 window.
    After 7:00: simulates from current 30min period to 13:00.
    consumption_profile: per-bucket overrides; fallback to CONSUMPTION_PER_30MIN.
    Returns target SOC percentage (minimum 10%).
    Weekend/holidays: always 10%.
    """
    if not is_workday:
        return MIN_SOC_PERCENT

    # Determine start: current 30min period or 7:00
    start_hour = 7
    start_minute = 0
    if now and now.hour >= 7:
        start_hour = now.hour
        start_minute = 0 if now.minute < 30 else 30

    cumulative_balance = 0.0
    min_balance = 0.0

    for period in forecast.forecast:
        dt = datetime.fromisoformat(period.period_start)
        hour = dt.hour
        minute = dt.minute
        if hour < start_hour or (hour == start_hour and minute < start_minute):
            continue
        if hour >= 13:
            continue

        pv_kwh_30min = period.pv_estimate_adjusted / 2  # rate -> kWh per 30min
        consumption = (
            consumption_profile.get(hour, minute) if consumption_profile else None
        )
        if consumption is None:
            consumption = CONSUMPTION_PER_30MIN
        balance = pv_kwh_30min - consumption
        cumulative_balance += balance
        min_balance = min(min_balance, cumulative_balance)

    if min_balance >= 0:
        return MIN_SOC_PERCENT

    deficit_kwh = abs(min_balance)
    deficit_percent = deficit_kwh / (BATTERY_CAPACITY_KWH / 100)
    target = MIN_SOC_PERCENT + deficit_percent * (1 + LOSS_FACTOR) + BUFFER_PERCENT

    return max(round(target), MIN_SOC_PERCENT)
