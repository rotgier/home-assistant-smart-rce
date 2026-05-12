"""Pure target SOC formula — extracted from pv_forecast.py.

Simulates cumulative energy deficit across the 7-13 window and returns
target battery SOC% plus per-bucket trace for observability. Single
source of truth for the formula + start_charge_hour clamp.

Reused by:
- `PvForecast._recalculate_target_soc` — single (PV strategy, Cons baseline)
  pairs (existing target_soc_* sensors).
- `pv_forecast_extrapolation` — extrapolated live variants.
- `domain/target_soc_matrix.compute_matrix` — full N×M matrix of strategy
  combinations.

Inputs (`AdjustedPvForecast`, `ConsumptionProfile`) are duck-typed —
their classes live in `pv_forecast.py` to avoid pulling the entire
PV-forecast vocabulary here. `TYPE_CHECKING` import keeps mypy/IDE
happy without runtime coupling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .pv_forecast import AdjustedPvForecast, ConsumptionProfile

# --- Constants --- #

CONSUMPTION_PER_30MIN: Final[float] = 0.45  # kWh (= 0.9 kWh/h / 2)
BATTERY_CAPACITY_KWH: Final[float] = 10.7
MIN_SOC_PERCENT: Final[int] = 10
LOSS_FACTOR: Final[float] = 0.10  # 10% conversion losses
BUFFER_PERCENT: Final[int] = 12


# --- Value objects --- #


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


# --- Pure function --- #


def calculate_target_soc(
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
    was pre-charge, cumulative_balance is clamped to <= 0 (deficit kept,
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
                consumption_profile.get(hour, minute) if consumption_profile else None
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

    return TargetSocResult(value=max(round(target), MIN_SOC_PERCENT), buckets=buckets)
