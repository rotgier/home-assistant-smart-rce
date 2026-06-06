"""Pure target SOC formula — extracted from pv_forecast.py.

Simulates cumulative energy deficit across the 7-13 window and returns
target battery SOC% plus per-bucket trace for observability. Single
source of truth for the formula + start_charge_hour clamp.

Reused by:
- `TargetSocCatalog._recalculate_target_soc` — single (PV strategy, Cons baseline)
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
from typing import TYPE_CHECKING, Final

from .bucket import Buckets

if TYPE_CHECKING:
    from .pv_forecast import ConsumptionProfile

# --- Constants --- #

CONSUMPTION_PER_30MIN: Final[float] = 0.45  # kWh (= 0.9 kWh/h / 2)
BATTERY_CAPACITY_KWH: Final[float] = 10.7
MIN_SOC_PERCENT: Final[int] = 10
LOSS_FACTOR: Final[float] = 0.10  # 10% conversion losses
BUFFER_PERCENT: Final[int] = 12


# --- Value objects --- #


@dataclass(frozen=True)
class PvProfile:
    """PV generation per 30-min bucket. Wraps a `Buckets` with PV-side role.

    Symmetric to `ConsumptionProfile`: strict 12-bucket contract over
    7:00..12:30 (validated inside `Buckets`), `.get(h, m)` returns float
    (no Optional). Build from an `AdjustedPvForecast` via
    `AdjustedPvForecast.to_profile(target_date)`.
    """

    buckets: Buckets

    def get(self, hour: int, minute: int) -> float:
        return self.buckets.get(hour, minute)

    @classmethod
    def flat(cls, value: float = 0.0) -> PvProfile:
        """Synthetic flat profile — every bucket = `value` kWh (default 0)."""
        return cls(buckets=Buckets.flat(value))


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
    pv_profile: PvProfile,
    consumption_profile: ConsumptionProfile,
    start_charge_hour: int | None = None,
) -> TargetSocResult:
    """Calculate target battery SOC + per-bucket trace.

    Pure cumulative-deficit sum over the 7:00..12:30 window. Each bucket
    contributes `pv_profile.get(h,m) - consumption_profile.get(h,m)`;
    the maximum cumulative deficit drives the SOC% needed.

    Time-awareness lives on the input profiles, not here — callers wanting
    "from now onwards" semantics pass profiles built via
    `AdjustedPvForecast.to_profile(target_date, now, pv_power_w_5min)` and
    `ConsumptionProfile.to_view(now, live_consumption_w)`. Those methods
    bake the in-progress bucket prorate / live override into the bucket
    values directly. For full-window (tomorrow, prev-day) callers use the
    plain forecast / historical profile.

    `start_charge_hour` (int | None): pre-charge gate. When set, surplus
    accumulated during pre-charge hours (hour < start_charge_hour) does
    not carry over to the next hour. Battery doesn't charge from PV in
    pre-charge (battery_charge_max_current_toggle=False) — hourly surplus
    is exported, not stored. At each hour boundary where the prior hour
    was pre-charge, cumulative_balance is clamped to <= 0 (deficit kept,
    surplus zeroed). See context/target_soc_algorithm.md option A.

    Returns TargetSocResult with .value (SOC percent) and .buckets (trace).
    """
    buckets: list[TargetSocBucket] = []
    cumulative_balance = 0.0
    min_balance = 0.0
    min_idx = -1
    prev_hour: int | None = None

    for hour in range(7, 13):
        for minute in (0, 30):
            # Hour-boundary clamp: if prior hour was in pre-charge, its surplus
            # was exported (not stored in battery) — zero out positive cumulative.
            if (
                prev_hour is not None
                and hour != prev_hour
                and start_charge_hour is not None
                and prev_hour < start_charge_hour
            ):
                cumulative_balance = min(cumulative_balance, 0.0)

            pv_kwh = pv_profile.get(hour, minute)
            cons_kwh = consumption_profile.get(hour, minute)
            balance = pv_kwh - cons_kwh
            cumulative_balance += balance
            if cumulative_balance < min_balance:
                min_balance = cumulative_balance
                min_idx = len(buckets)
            buckets.append(
                TargetSocBucket(
                    period=f"{hour:02d}:{minute:02d}",
                    pv_kwh=round(pv_kwh, 3),
                    cons_kwh=round(cons_kwh, 3),
                    balance=round(balance, 3),
                    cumulative=round(cumulative_balance, 3),
                    is_min=False,  # set below
                )
            )
            prev_hour = hour

    if min_idx >= 0:
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
