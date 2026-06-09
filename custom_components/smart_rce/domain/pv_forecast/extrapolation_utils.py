"""Cross-cutting helpers shared by all 4 EXTRAP strategies.

`_weighted_score_over_buckets` is the shared core that walks back from
the current bucket applying a per-strategy `score_fn`; per-variant
algorithm details (score computation, future projection) live as
`@staticmethod` on each `Extrap*Strategy` in `strategies_extrapolation.py`.
`_assemble` patches the in-progress + future buckets onto a fresh
LIVE result via `PvForecastResult.with_now_aware_in_progress_and_future_overrides`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ..bucket import Bucket
from .strategy_base import PvForecastResult, SolcastPeriod

# Exponential decay factor for the weighted scoring core — current bucket
# weight 1.0; each step back multiplies by this. 0.7 → after 3 buckets ≈ 0.34.
PATTERN_DECAY: float = 0.7

# Minimum forecast PV (kWh per 30min) for a past bucket to contribute to the
# score average. Filters pre-dawn / post-dusk hours where `(estimate - p10)`
# is near zero → division noise.
PATTERN_MIN_FORECAST_KWH: float = 0.05


def assemble(
    pv_forecast_live: PvForecastResult,
    now: datetime,
    *,
    pv_bucket_so_far_kwh: float,
    pv_power_w_5min: float,
    future_overrides: dict[tuple[int, int], float],
) -> PvForecastResult:
    """Build an EXTRAP result from a strategy's per-bucket overrides.

    Single source of truth for the in-progress bucket rate is
    `PvForecastResult.with_now_aware_in_progress_and_future_overrides`,
    which uses `Bucket.full_bucket_kwh × 2` (kWh/h). Future periods
    take `future_overrides[(h,m)]` when present, else keep their original
    forecast values. Past periods are untouched.

    target_soc + remaining_kwh derivation is external (TargetSocCatalog
    + `PvForecastResult.remaining_kwh_from(now)`); this function returns
    only the patched forecast.
    """
    return pv_forecast_live.with_now_aware_in_progress_and_future_overrides(
        now=now,
        pv_power_w_5min=pv_power_w_5min,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        future_pv_kwh_per_h_overrides=future_overrides,
    )


def index_solcast_by_bucket(
    solcast_today: list[SolcastPeriod], now: datetime
) -> dict[tuple[int, int], SolcastPeriod]:
    """Filter solcast to today's periods, index by (hour, minute) for O(1) lookup."""
    out: dict[tuple[int, int], SolcastPeriod] = {}
    for sp in solcast_today:
        dt = datetime.fromisoformat(sp.period_start)
        if dt.date() != now.date():
            continue
        out[(dt.hour, dt.minute)] = sp
    return out


def weighted_score_over_buckets(
    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
    now: datetime,
    realized_pv_today: dict[tuple[int, int], float],
    pv_bucket_so_far_kwh: float,
    pv_power_w_5min: float,
    score_fn: Callable[[float, SolcastPeriod], float | None],
    max_age: int,
) -> float | None:
    """Walk back from current bucket, compute per-bucket score, weight by decay.

    Shared core of all four `_compute_weighted_*_score` methods — they
    differ only in `score_fn` (4-zone, proportional, band) and `max_age`
    (24 for full-history, 1 for band_recent).

    Current bucket realized rate uses `_current_bucket_realized_rate`
    (uniform with chart + target_soc). Past buckets use
    `realized_pv_today[(h, m)] × 2`. Buckets with
    `pv_estimate / 2 < PATTERN_MIN_FORECAST_KWH` skipped.
    """
    current_hour = now.hour
    current_minute = 0 if now.minute < 30 else 30
    scores: list[tuple[float, float]] = []
    age = 0
    weight = 1.0
    h, m = current_hour, current_minute
    while True:
        sp = solcast_by_bucket.get((h, m))
        if sp is None:
            break
        if sp.pv_estimate / 2 < PATTERN_MIN_FORECAST_KWH:
            h, m, age, weight = _step_back(h, m, age, weight)
            if age > max_age:
                break
            continue
        if (h, m) == (current_hour, current_minute):
            realized_rate = _current_bucket_realized_rate(
                now, pv_power_w_5min, pv_bucket_so_far_kwh
            )
        else:
            realized_kwh = realized_pv_today.get((h, m))
            if realized_kwh is None:
                h, m, age, weight = _step_back(h, m, age, weight)
                if age > max_age:
                    break
                continue
            realized_rate = realized_kwh * 2
        score = score_fn(realized_rate, sp)
        if score is not None:
            scores.append((score, weight))
        h, m, age, weight = _step_back(h, m, age, weight)
        if age > max_age:
            break

    if not scores:
        return None
    total_weight = sum(w for _, w in scores)
    return sum(s * w for s, w in scores) / total_weight


def _current_bucket_realized_rate(
    now: datetime, pv_power_w_5min: float, pv_bucket_so_far_kwh: float
) -> float:
    """In-progress bucket realized rate (kWh/h) for score computation.

    Uniform across all variants — same source of truth as chart and
    target_soc paths: `Bucket.full_bucket_kwh(now, pv_w, so_far) × 2`
    (kWh per 30-min × 2 = kWh/h).
    """
    return Bucket.full_bucket_kwh(now, pv_power_w_5min, pv_bucket_so_far_kwh) * 2.0


def _step_back(h: int, m: int, age: int, weight: float) -> tuple[int, int, int, float]:
    """Move 30 min back; decay weight."""
    if m == 30:
        return h, 0, age + 1, weight * PATTERN_DECAY
    return (h - 1) if h > 0 else 0, 30, age + 1, weight * PATTERN_DECAY
