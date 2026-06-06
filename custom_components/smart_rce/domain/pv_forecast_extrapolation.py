"""Extrapolated PV forecast variants — strategies for future buckets.

Each `extrapolate_*` function returns a `PvForecastResult` with the
in-progress bucket rescaled + future buckets patched per strategy. The
EXTRAP `ForecastStrategy` subclasses (in `pv_forecast_strategy.py`)
call these and store the result; `TargetSocCatalog` derives target_soc
externally; `PvForecastResult.remaining_kwh_from(now)` derives remaining
kWh externally.

In-progress bucket handling is uniform across all variants — built
on `Bucket.full_bucket_kwh(now, pv_power_w_5min, pv_bucket_so_far_kwh)`
which yields realized so-far plus 5-min power × remaining time. That same
value drives:
- chart display (in-progress period rescaled to `full_bucket_kwh × 2` rate
  via `PvForecastResult.with_now_aware_in_progress_and_future_overrides`)
- strategy `score` input (`realized_rate = full_bucket_kwh × 2`)
- target_soc input via `PvForecastResult.to_profile(now, pv_w_5min)`
  (uses `live_remaining_kwh` part only — past contributes 0 to the
  forward-looking deficit). Computed by `TargetSocCatalog`, not here.

The variants differ only in how they project FUTURE buckets:

1. calibrated_pattern   : weighted 4-zone realization score (p10/est/p90)
                          mapped through inverse score scale onto future
                          per-bucket Solcast forecasts.
2. proportional_median  : `S = (real-est) / est` (band-width independent).
                          Future rate = est x (1 + cumS), floor at -0.95.
3. band_clamped         : 2-zone score anchored at [p10, p90], clamped
                          above p90. Future rate = p10 + cumS x (p90-p10).
4. band_clamped_recent  : same as band_clamped but lookback narrowed to
                          current bucket + 1 prior (short-horizon trend
                          without morning bias).
"""

from __future__ import annotations

from datetime import datetime

from .bucket import Bucket
from .pv_forecast import PvForecastResult, SolcastPeriod

# Exponential decay factor for the calibrated pattern variant — current bucket
# weight 1.0; each step back multiplies by this. 0.7 → after 3 buckets ≈ 0.34.
PATTERN_DECAY: float = 0.7

# Minimum forecast PV (kWh per 30min) for a past bucket to contribute to the
# pattern factor. Filters pre-dawn / post-dusk hours where (estimate - p10)
# is near zero → division noise.
PATTERN_MIN_FORECAST_KWH: float = 0.05


def extrapolate_calibrated_pattern(
    adjusted_live: PvForecastResult,
    solcast_live: list[SolcastPeriod],
    now: datetime,
    pv_bucket_so_far_kwh: float | None,
    realized_pv_today: dict[tuple[int, int], float],
    pv_power_w_5min: float | None,
) -> PvForecastResult | None:
    """Variant — projects realization score from past buckets onto future.

    Each past + current bucket gets a normalized score on a 4-zone scale
    using Solcast's three quantiles (p10, estimate=p50, p90):

        S < 0     : realized < p10        S = realized/p10 - 1   (range -1..0)
        S in 0..1 : p10 ≤ realized ≤ est  S = (real-p10)/(est-p10)
        S in 1..2 : est < realized ≤ p90  S = 1 + (real-est)/(p90-est)
        S > 2     : realized > p90        S = 2 + (real-p90)/p90

    Past buckets read from `realized_pv_today` (utility meter history per
    closed bucket). Current bucket realized rate = `full_bucket_kwh × 2`
    (same source of truth as chart + target_soc). Buckets with
    `pv_estimate / 2 < PATTERN_MIN_FORECAST_KWH` are skipped (pre-dawn /
    post-dusk noise).

    Weighted average score (current = weight 1.0, each step back × PATTERN_DECAY)
    is mapped back through the inverse of the same 4-zone scale to project
    each future bucket's PV rate.
    """
    if pv_bucket_so_far_kwh is None or pv_power_w_5min is None:
        return None

    solcast_by_bucket = _index_solcast_by_bucket(solcast_live, now)
    score = _compute_weighted_score(
        solcast_by_bucket=solcast_by_bucket,
        now=now,
        realized_pv_today=realized_pv_today,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        pv_power_w_5min=pv_power_w_5min,
    )
    if score is None:
        return None

    future_overrides = _project_future_buckets(
        solcast_by_bucket=solcast_by_bucket, now=now, score=score
    )
    return _assemble(
        adjusted_live=adjusted_live,
        now=now,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        pv_power_w_5min=pv_power_w_5min,
        future_overrides=future_overrides,
    )


# Minimum rate gap (kWh/h) for ratio division to be considered stable.
# Below this, fall back to ratio against a wider quantile.
_RATE_EPS: float = 0.05


def _compute_score(
    realized_rate: float,
    p10_rate: float,
    est_rate: float,
    p90_rate: float,
) -> float | None:
    """Realization score on 4-zone normalized scale (rates in kWh/h)."""
    if realized_rate < p10_rate:
        if p10_rate >= _RATE_EPS:
            return realized_rate / p10_rate - 1.0
        if est_rate >= _RATE_EPS:
            return realized_rate / est_rate - 1.0
        return None
    if realized_rate <= est_rate:
        if (est_rate - p10_rate) >= _RATE_EPS:
            return (realized_rate - p10_rate) / (est_rate - p10_rate)
        return 0.5
    if realized_rate <= p90_rate:
        if (p90_rate - est_rate) >= _RATE_EPS:
            return 1.0 + (realized_rate - est_rate) / (p90_rate - est_rate)
        if est_rate >= _RATE_EPS:
            return 1.0 + (realized_rate - est_rate) / est_rate
        return None
    if p90_rate >= _RATE_EPS:
        return 2.0 + (realized_rate - p90_rate) / p90_rate
    return None


def _project_rate_from_score(
    p10_rate: float,
    est_rate: float,
    p90_rate: float,
    score: float,
) -> float:
    """Inverse of _compute_score — given score, project PV rate (kWh/h)."""
    if score < 0.0:
        return max(0.0, p10_rate * (1.0 + score))
    if score <= 1.0:
        return p10_rate + score * (est_rate - p10_rate)
    if score <= 2.0:
        if (p90_rate - est_rate) >= _RATE_EPS:
            return est_rate + (score - 1.0) * (p90_rate - est_rate)
        return est_rate * (1.0 + (score - 1.0))
    return p90_rate * (1.0 + (score - 2.0))


def _current_bucket_realized_rate(
    now: datetime, pv_power_w_5min: float, pv_bucket_so_far_kwh: float
) -> float:
    """In-progress bucket realized rate (kWh/h) for score computation.

    Uniform across all variants — same source of truth as chart and
    target_soc paths: `Bucket.full_bucket_kwh(now, pv_w, so_far) × 2`
    (kWh per 30-min × 2 = kWh/h).
    """
    return Bucket.full_bucket_kwh(now, pv_power_w_5min, pv_bucket_so_far_kwh) * 2.0


def _compute_weighted_score(
    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
    now: datetime,
    realized_pv_today: dict[tuple[int, int], float],
    pv_bucket_so_far_kwh: float,
    pv_power_w_5min: float,
) -> float | None:
    """Weighted realization score over today's past + current daylight buckets.

    Current bucket realized_rate via `_current_bucket_realized_rate`; past
    buckets via `realized_pv_today[(h, m)] × 2`. Decaying weight: current=1.0,
    each step back × PATTERN_DECAY.
    """
    return _weighted_score_over_buckets(
        solcast_by_bucket=solcast_by_bucket,
        now=now,
        realized_pv_today=realized_pv_today,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        pv_power_w_5min=pv_power_w_5min,
        score_fn=lambda real, sp: _compute_score(
            real, sp.pv_estimate10, sp.pv_estimate, sp.pv_estimate90
        ),
        max_age=24,
    )


def _step_back(h: int, m: int, age: int, weight: float) -> tuple[int, int, int, float]:
    """Move 30 min back; decay weight."""
    if m == 30:
        return h, 0, age + 1, weight * PATTERN_DECAY
    return (h - 1) if h > 0 else 0, 30, age + 1, weight * PATTERN_DECAY


def _compute_proportional_score(realized_rate: float, est_rate: float) -> float | None:
    """Score = (realized - est) / est. Centered at 0 (S=0 → real=est)."""
    if est_rate < _RATE_EPS:
        return None
    return (realized_rate - est_rate) / est_rate


# Clamp for negative cumS in projection — prevent project=0 when cumS=-1.
_PROPORTIONAL_FLOOR: float = -0.95


def _compute_weighted_proportional_score(
    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
    now: datetime,
    realized_pv_today: dict[tuple[int, int], float],
    pv_bucket_so_far_kwh: float,
    pv_power_w_5min: float,
) -> float | None:
    """Weighted (decay 0.7) average of proportional scores over today's buckets."""
    return _weighted_score_over_buckets(
        solcast_by_bucket=solcast_by_bucket,
        now=now,
        realized_pv_today=realized_pv_today,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        pv_power_w_5min=pv_power_w_5min,
        score_fn=lambda real, sp: _compute_proportional_score(real, sp.pv_estimate),
        max_age=24,
    )


def _project_future_buckets_proportional(
    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
    now: datetime,
    cum_s: float,
) -> dict[tuple[int, int], float]:
    """For each future bucket, project rate = est × (1 + cumS)."""
    current_hour = now.hour
    current_minute = 0 if now.minute < 30 else 30
    clamped = max(cum_s, _PROPORTIONAL_FLOOR)
    overrides: dict[tuple[int, int], float] = {}
    for (h, m), sp in solcast_by_bucket.items():
        if h < current_hour or (h == current_hour and m <= current_minute):
            continue
        overrides[(h, m)] = max(0.0, sp.pv_estimate * (1.0 + clamped))
    return overrides


def extrapolate_proportional_median(
    adjusted_live: PvForecastResult,
    solcast_live: list[SolcastPeriod],
    now: datetime,
    pv_bucket_so_far_kwh: float | None,
    realized_pv_today: dict[tuple[int, int], float],
    pv_power_w_5min: float | None,
) -> PvForecastResult | None:
    """Variant — proportional-to-median realization scaling.

    Same shape as `extrapolate_calibrated_pattern` but uses
    `S = (realized - est) / est` (band-width independent). Future rate =
    `est × (1 + cumS)`, floored at cumS=-0.95.
    """
    if pv_bucket_so_far_kwh is None or pv_power_w_5min is None:
        return None

    solcast_by_bucket = _index_solcast_by_bucket(solcast_live, now)
    cum_s = _compute_weighted_proportional_score(
        solcast_by_bucket=solcast_by_bucket,
        now=now,
        realized_pv_today=realized_pv_today,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        pv_power_w_5min=pv_power_w_5min,
    )
    if cum_s is None:
        return None

    future_overrides = _project_future_buckets_proportional(
        solcast_by_bucket=solcast_by_bucket, now=now, cum_s=cum_s
    )
    return _assemble(
        adjusted_live=adjusted_live,
        now=now,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        pv_power_w_5min=pv_power_w_5min,
        future_overrides=future_overrides,
    )


def _compute_band_score(
    realized_rate: float, p10_rate: float, p90_rate: float
) -> float | None:
    """Score on 2-zone scale anchored by [p10, p90]; clamped at 1 above p90."""
    if realized_rate >= p90_rate:
        return 1.0
    if realized_rate >= p10_rate:
        if (p90_rate - p10_rate) >= _RATE_EPS:
            return (realized_rate - p10_rate) / (p90_rate - p10_rate)
        return 0.5
    if p10_rate >= _RATE_EPS:
        return realized_rate / p10_rate - 1.0
    return None


def _compute_weighted_band_score(
    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
    now: datetime,
    realized_pv_today: dict[tuple[int, int], float],
    pv_bucket_so_far_kwh: float,
    pv_power_w_5min: float,
) -> float | None:
    """Weighted (decay 0.7) band-clamped score over today's daylight buckets."""
    return _weighted_score_over_buckets(
        solcast_by_bucket=solcast_by_bucket,
        now=now,
        realized_pv_today=realized_pv_today,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        pv_power_w_5min=pv_power_w_5min,
        score_fn=lambda real, sp: _compute_band_score(
            real, sp.pv_estimate10, sp.pv_estimate90
        ),
        max_age=24,
    )


def _project_future_buckets_band(
    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
    now: datetime,
    cum_s: float,
) -> dict[tuple[int, int], float]:
    """For each future bucket: project = p10 + S × (p90 − p10), bounded ≥ 0."""
    current_hour = now.hour
    current_minute = 0 if now.minute < 30 else 30
    overrides: dict[tuple[int, int], float] = {}
    for (h, m), sp in solcast_by_bucket.items():
        if h < current_hour or (h == current_hour and m <= current_minute):
            continue
        p10, p90 = sp.pv_estimate10, sp.pv_estimate90
        if cum_s < 0:
            projected = max(0.0, p10 * (1.0 + cum_s))
        elif cum_s <= 1.0:
            projected = p10 + cum_s * (p90 - p10)
        else:
            projected = p90
        overrides[(h, m)] = max(0.0, projected)
    return overrides


def extrapolate_band_clamped(
    adjusted_live: PvForecastResult,
    solcast_live: list[SolcastPeriod],
    now: datetime,
    pv_bucket_so_far_kwh: float | None,
    realized_pv_today: dict[tuple[int, int], float],
    pv_power_w_5min: float | None,
) -> PvForecastResult | None:
    """Variant — 2-zone band-clamped realization scaling."""
    if pv_bucket_so_far_kwh is None or pv_power_w_5min is None:
        return None

    solcast_by_bucket = _index_solcast_by_bucket(solcast_live, now)
    cum_s = _compute_weighted_band_score(
        solcast_by_bucket=solcast_by_bucket,
        now=now,
        realized_pv_today=realized_pv_today,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        pv_power_w_5min=pv_power_w_5min,
    )
    if cum_s is None:
        return None

    future_overrides = _project_future_buckets_band(
        solcast_by_bucket=solcast_by_bucket, now=now, cum_s=cum_s
    )
    return _assemble(
        adjusted_live=adjusted_live,
        now=now,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        pv_power_w_5min=pv_power_w_5min,
        future_overrides=future_overrides,
    )


# Max age (steps back from current) for the "recent" band variant — only
# current bucket + 1 prior bucket contribute. Captures short-horizon weather
# trend without morning bias.
BAND_RECENT_MAX_AGE: int = 1


def _compute_weighted_band_score_recent(
    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
    now: datetime,
    realized_pv_today: dict[tuple[int, int], float],
    pv_bucket_so_far_kwh: float,
    pv_power_w_5min: float,
) -> float | None:
    """Compute band score weighted average with narrow lookback (current + 1 back)."""
    return _weighted_score_over_buckets(
        solcast_by_bucket=solcast_by_bucket,
        now=now,
        realized_pv_today=realized_pv_today,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        pv_power_w_5min=pv_power_w_5min,
        score_fn=lambda real, sp: _compute_band_score(
            real, sp.pv_estimate10, sp.pv_estimate90
        ),
        max_age=BAND_RECENT_MAX_AGE,
    )


def extrapolate_band_clamped_recent(
    adjusted_live: PvForecastResult,
    solcast_live: list[SolcastPeriod],
    now: datetime,
    pv_bucket_so_far_kwh: float | None,
    realized_pv_today: dict[tuple[int, int], float],
    pv_power_w_5min: float | None,
) -> PvForecastResult | None:
    """Variant — band-clamped scoring, narrow lookback (current + 1 back).

    Same band-clamped score as `extrapolate_band_clamped` but limited to
    BAND_RECENT_MAX_AGE=1 — only the current bucket and the immediately
    prior bucket contribute to cumS. Captures current weather trend
    without carrying morning bias into afternoon projections.
    """
    if pv_bucket_so_far_kwh is None or pv_power_w_5min is None:
        return None

    solcast_by_bucket = _index_solcast_by_bucket(solcast_live, now)
    cum_s = _compute_weighted_band_score_recent(
        solcast_by_bucket=solcast_by_bucket,
        now=now,
        realized_pv_today=realized_pv_today,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        pv_power_w_5min=pv_power_w_5min,
    )
    if cum_s is None:
        return None

    future_overrides = _project_future_buckets_band(
        solcast_by_bucket=solcast_by_bucket, now=now, cum_s=cum_s
    )
    return _assemble(
        adjusted_live=adjusted_live,
        now=now,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        pv_power_w_5min=pv_power_w_5min,
        future_overrides=future_overrides,
    )


def _project_future_buckets(
    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
    now: datetime,
    score: float,
) -> dict[tuple[int, int], float]:
    """For each future bucket, project PV rate via inverse 4-zone score scale."""
    current_hour = now.hour
    current_minute = 0 if now.minute < 30 else 30
    overrides: dict[tuple[int, int], float] = {}
    for (h, m), sp in solcast_by_bucket.items():
        if h < current_hour or (h == current_hour and m <= current_minute):
            continue
        projected_rate = _project_rate_from_score(
            p10_rate=sp.pv_estimate10,
            est_rate=sp.pv_estimate,
            p90_rate=sp.pv_estimate90,
            score=score,
        )
        overrides[(h, m)] = max(0.0, projected_rate)
    return overrides


def _index_solcast_by_bucket(
    solcast_live: list[SolcastPeriod], now: datetime
) -> dict[tuple[int, int], SolcastPeriod]:
    """Filter solcast to today's periods, index by (hour, minute) for O(1) lookup."""
    out: dict[tuple[int, int], SolcastPeriod] = {}
    for sp in solcast_live:
        dt = datetime.fromisoformat(sp.period_start)
        if dt.date() != now.date():
            continue
        out[(dt.hour, dt.minute)] = sp
    return out


def _weighted_score_over_buckets(
    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
    now: datetime,
    realized_pv_today: dict[tuple[int, int], float],
    pv_bucket_so_far_kwh: float,
    pv_power_w_5min: float,
    score_fn,
    max_age: int,
) -> float | None:
    """Walk back from current bucket, compute per-bucket score, weight by decay.

    Shared core of all four `_compute_weighted_*_score` functions — they
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


def _assemble(
    adjusted_live: PvForecastResult,
    now: datetime,
    *,
    pv_bucket_so_far_kwh: float,
    pv_power_w_5min: float,
    future_overrides: dict[tuple[int, int], float],
) -> PvForecastResult:
    """Build the EXTRAP result from a strategy's per-bucket overrides.

    Single source of truth for the in-progress bucket rate is
    `PvForecastResult.with_now_aware_in_progress_and_future_overrides`,
    which uses `Bucket.full_bucket_kwh × 2` (kWh/h). Future periods
    take `future_overrides[(h,m)]` when present, else keep their original
    forecast values. Past periods are untouched.

    target_soc + remaining_kwh derivation is external (TargetSocCatalog
    + `PvForecastResult.remaining_kwh_from(now)`); this function returns
    only the patched forecast.
    """
    return adjusted_live.with_now_aware_in_progress_and_future_overrides(
        now=now,
        pv_power_w_5min=pv_power_w_5min,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        future_pv_kwh_per_h_overrides=future_overrides,
    )
