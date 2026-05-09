"""Extrapolated PV forecast variants — three strategies for in-progress + future buckets.

Each strategy produces an ExtrapolatedLive bundle:
- adjusted: full per-period AdjustedPvForecast (chart attribute source)
- remaining_kwh: scalar sum from now to end-of-day (sensor state)
- target_soc: SOC % needed for 7-13 deficit window (sensor value)

Strategies (current-bucket source → future-bucket source):
1. realized_prorate   : utility meter so-far / elapsed   → forecast unchanged
2. live_5min_rate     : 5-min average power sensors      → forecast unchanged
3. calibrated_pattern : weighted realization factor      → factor applied to
                        per-bucket [pv_estimate10, pv_estimate] solcast range

All three use CONSUMPTION_PER_30MIN constant (consumption_profile=None) for
non-current-bucket consumption inside _calculate_target_soc, matching the
target_soc_live baseline.
"""

from __future__ import annotations

from datetime import datetime

from .pv_forecast import (
    CONSUMPTION_PER_30MIN,
    AdjustedPeriod,
    AdjustedPvForecast,
    ExtrapolatedLive,
    PvForecast,
    SolcastPeriod,
)

# Min minutes elapsed before we trust realized prorate / pattern variants.
# Below this, division by tiny elapsed_min produces noise. Same threshold
# (3 min) as the dashboard's extrapolate_current_bucket_js MIN_ELAPSED_MS.
MIN_ELAPSED_FOR_REALIZED_PRORATE: int = 3

# Exponential decay factor for the calibrated pattern variant — current bucket
# weight 1.0; each step back multiplies by this. 0.7 → after 3 buckets ≈ 0.34.
PATTERN_DECAY: float = 0.7

# Minimum forecast PV (kWh per 30min) for a past bucket to contribute to the
# pattern factor. Filters pre-dawn / post-dusk hours where (estimate - p10)
# is near zero → division noise.
PATTERN_MIN_FORECAST_KWH: float = 0.05


def extrapolate_realized_prorate(
    adjusted_live: AdjustedPvForecast,
    now: datetime,
    pv_bucket_so_far_kwh: float | None,
    consumption_bucket_so_far_kwh: float | None,
) -> ExtrapolatedLive:
    """Variant 1 — full bucket projection from utility-meter so-far values.

    Same logic as the dashboard PV Gen / Cons -Water chart series:
        rate kWh/h    = realized × 60 / elapsed_min
        remaining kWh = realized × remaining_min / elapsed_min
    """
    elapsed_min = now.minute % 30
    remaining_min = 30 - elapsed_min
    if (
        elapsed_min < MIN_ELAPSED_FOR_REALIZED_PRORATE
        or pv_bucket_so_far_kwh is None
        or consumption_bucket_so_far_kwh is None
    ):
        return ExtrapolatedLive.empty()

    current_pv_rate = pv_bucket_so_far_kwh * 60 / elapsed_min
    current_pv_remaining_kwh = pv_bucket_so_far_kwh * remaining_min / elapsed_min
    current_cons_remaining_kwh = (
        consumption_bucket_so_far_kwh * remaining_min / elapsed_min
    )
    return _build_result(
        adjusted_live,
        now,
        current_bucket_pv_kwh_per_h=current_pv_rate,
        current_pv_remaining_kwh=current_pv_remaining_kwh,
        current_cons_remaining_kwh=current_cons_remaining_kwh,
    )


def extrapolate_5min_rate(
    adjusted_live: AdjustedPvForecast,
    now: datetime,
    pv_power_w: float | None,
    consumption_w: float | None,
) -> ExtrapolatedLive:
    """Variant 2 — current bucket replaced by 5-min average power sensor reading.

    Useful when utility meter is unavailable / too early in bucket — 5-min
    average has shorter window so reflects current rate even at bucket start.
    """
    if pv_power_w is None or consumption_w is None:
        return ExtrapolatedLive.empty()

    remaining_min = 30 - now.minute % 30
    pv_rate_kwh_per_h = pv_power_w / 1000  # W → kWh/h rate (= kW)
    current_pv_remaining_kwh = pv_power_w / 1000 * remaining_min / 60
    current_cons_remaining_kwh = consumption_w / 1000 * remaining_min / 60
    return _build_result(
        adjusted_live,
        now,
        current_bucket_pv_kwh_per_h=pv_rate_kwh_per_h,
        current_pv_remaining_kwh=current_pv_remaining_kwh,
        current_cons_remaining_kwh=current_cons_remaining_kwh,
    )


def extrapolate_calibrated_pattern(
    adjusted_live: AdjustedPvForecast,
    solcast_live: list[SolcastPeriod],
    now: datetime,
    pv_bucket_so_far_kwh: float | None,
    consumption_bucket_so_far_kwh: float | None,
    realized_pv_today: dict[tuple[int, int], float],
) -> ExtrapolatedLive:
    """Variant 3 — projects realization score from past buckets onto future.

    Each past + current bucket gets a normalized score on a 4-zone scale
    using Solcast's three quantiles (p10, estimate=p50, p90):

        S < 0     : realized < p10        S = realized/p10 - 1   (range -1..0)
        S in 0..1 : p10 ≤ realized ≤ est  S = (real-p10)/(est-p10)
        S in 1..2 : est < realized ≤ p90  S = 1 + (real-est)/(p90-est)
        S > 2     : realized > p90        S = 2 + (real-p90)/p90

    Score is dimensionless and continuous across zones. Below p10 we use a
    ratio (instead of unbounded linear extrapolation) so projection never
    goes to 0 unless realized is exactly 0. Above p90 we use a ratio for
    similar bounded behavior.

    Past buckets read from `realized_pv_today` (utility meter history per
    closed bucket). Current bucket extrapolated from `pv_bucket_so_far_kwh /
    elapsed × 30`. Buckets with `pv_estimate / 2 < PATTERN_MIN_FORECAST_KWH`
    are skipped (pre-dawn / post-dusk noise).

    Weighted average score (current = weight 1.0, each step back × PATTERN_DECAY)
    is mapped back through the inverse of the same 4-zone scale to project
    each future bucket's PV rate. The projected per-bucket rate replaces
    forecast PV; consumption uses the same prorate as variant 1 for the
    current bucket and CONSUMPTION_PER_30MIN for future buckets.
    """
    elapsed_min = now.minute % 30
    remaining_min = 30 - elapsed_min
    if (
        elapsed_min < MIN_ELAPSED_FOR_REALIZED_PRORATE
        or pv_bucket_so_far_kwh is None
        or consumption_bucket_so_far_kwh is None
    ):
        return ExtrapolatedLive.empty()

    # Index solcast periods by (hour, minute) for O(1) lookup.
    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod] = {}
    for sp in solcast_live:
        dt = datetime.fromisoformat(sp.period_start)
        if dt.date() != now.date():
            continue
        solcast_by_bucket[(dt.hour, dt.minute)] = sp

    score = _compute_weighted_score(
        solcast_by_bucket=solcast_by_bucket,
        now=now,
        realized_pv_today=realized_pv_today,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        elapsed_min=elapsed_min,
    )
    if score is None:
        return ExtrapolatedLive.empty()

    # Current bucket: same as variant 1 (realized prorate).
    current_pv_rate = pv_bucket_so_far_kwh * 60 / elapsed_min
    current_pv_remaining_kwh = pv_bucket_so_far_kwh * remaining_min / elapsed_min
    current_cons_remaining_kwh = (
        consumption_bucket_so_far_kwh * remaining_min / elapsed_min
    )

    # Future buckets: project rate using inverse 4-zone score mapping.
    future_overrides = _project_future_buckets(
        solcast_by_bucket=solcast_by_bucket, now=now, score=score
    )
    adjusted = _build_extrapolated_forecast(
        adjusted_live,
        now,
        current_bucket_pv_kwh_per_h=current_pv_rate,
        future_pv_kwh_per_h_overrides=future_overrides,
    )
    remaining_kwh = _sum_remaining_kwh(adjusted, now)
    target_soc = PvForecast._calculate_target_soc(  # noqa: SLF001 — same-package use
        adjusted,
        consumption_profile=None,
        now=now,
        current_bucket_override=(
            current_pv_remaining_kwh,
            current_cons_remaining_kwh,
        ),
    )
    return ExtrapolatedLive(
        adjusted=adjusted, remaining_kwh=remaining_kwh, target_soc=target_soc
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
    """Realization score on 4-zone normalized scale (rates in kWh/h).

    Returns:
        S < 0     : realized < p10  → S = realized/p10 - 1   (range -1..0)
        S in 0..1 : p10..est        → S = (real-p10)/(est-p10)
        S in 1..2 : est..p90        → S = 1 + (real-est)/(p90-est)
        S > 2     : realized > p90  → S = 2 + (real-p90)/p90
        None      : insufficient data (rates too small for stable ratio)

    """
    # Below p10 — ratio against p10 (or estimate as fallback if p10 too small)
    if realized_rate < p10_rate:
        if p10_rate >= _RATE_EPS:
            return realized_rate / p10_rate - 1.0
        if est_rate >= _RATE_EPS:
            return realized_rate / est_rate - 1.0
        return None
    # p10..estimate — linear interpolation
    if realized_rate <= est_rate:
        if (est_rate - p10_rate) >= _RATE_EPS:
            return (realized_rate - p10_rate) / (est_rate - p10_rate)
        # Collapsed zone (est ≈ p10) — fall through to next zone via shifted score
        return 0.5
    # estimate..p90 — linear interpolation
    if realized_rate <= p90_rate:
        if (p90_rate - est_rate) >= _RATE_EPS:
            return 1.0 + (realized_rate - est_rate) / (p90_rate - est_rate)
        # Collapsed zone (p90 ≈ est) — use ratio over estimate
        if est_rate >= _RATE_EPS:
            return 1.0 + (realized_rate - est_rate) / est_rate
        return None
    # Above p90 — ratio over p90
    if p90_rate >= _RATE_EPS:
        return 2.0 + (realized_rate - p90_rate) / p90_rate
    return None


def _project_rate_from_score(
    p10_rate: float,
    est_rate: float,
    p90_rate: float,
    score: float,
) -> float:
    """Inverse of _compute_score — given score, project PV rate (kWh/h).

    Bounded below at 0 (negative scores asymptote toward 0 at score=-1).
    Unbounded above (score > 2 ratio-based, can exceed p90).
    """
    if score < 0.0:
        # Below p10: scale by (1+score). score=-1 → 0; score=0 → p10
        return max(0.0, p10_rate * (1.0 + score))
    if score <= 1.0:
        return p10_rate + score * (est_rate - p10_rate)
    if score <= 2.0:
        if (p90_rate - est_rate) >= _RATE_EPS:
            return est_rate + (score - 1.0) * (p90_rate - est_rate)
        # Collapsed zone — ratio over estimate
        return est_rate * (1.0 + (score - 1.0))
    # Above p90 — ratio
    return p90_rate * (1.0 + (score - 2.0))


def _compute_weighted_score(
    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
    now: datetime,
    realized_pv_today: dict[tuple[int, int], float],
    pv_bucket_so_far_kwh: float,
    elapsed_min: int,
) -> float | None:
    """Weighted realization score over today's past + current daylight buckets.

    Each bucket contributes a 4-zone score (see _compute_score) and a decaying
    weight (current bucket = 1.0, each step back × PATTERN_DECAY). Final score
    is the weighted average.
    """
    current_hour = now.hour
    current_minute = 0 if now.minute < 30 else 30
    scores: list[tuple[float, float]] = []  # (score, weight)
    age = 0
    weight = 1.0
    h, m = current_hour, current_minute
    while True:
        sp = solcast_by_bucket.get((h, m))
        if sp is None:
            break
        # Skip pre-dawn / post-dusk buckets (estimate too small for reliable score).
        # PATTERN_MIN_FORECAST_KWH is per-bucket kWh; pv_estimate is kWh/h rate so /2.
        if sp.pv_estimate / 2 < PATTERN_MIN_FORECAST_KWH:
            h, m, age, weight = _step_back(h, m, age, weight)
            if age > 24:
                break
            continue
        # Realized rate (kWh/h) for this bucket.
        if (h, m) == (current_hour, current_minute):
            # Current bucket — extrapolate so-far to full bucket equivalent rate.
            realized_rate = pv_bucket_so_far_kwh * 60 / elapsed_min
        else:
            realized_kwh = realized_pv_today.get((h, m))
            if realized_kwh is None:
                h, m, age, weight = _step_back(h, m, age, weight)
                if age > 24:
                    break
                continue
            realized_rate = realized_kwh * 2  # kWh per 30min → kWh/h
        score = _compute_score(
            realized_rate=realized_rate,
            p10_rate=sp.pv_estimate10,
            est_rate=sp.pv_estimate,
            p90_rate=sp.pv_estimate90,
        )
        if score is not None:
            scores.append((score, weight))
        h, m, age, weight = _step_back(h, m, age, weight)
        if age > 24:
            break

    if not scores:
        return None
    total_weight = sum(w for _, w in scores)
    return sum(s * w for s, w in scores) / total_weight


def _step_back(h: int, m: int, age: int, weight: float) -> tuple[int, int, int, float]:
    """Move 30 min back; decay weight."""
    if m == 30:
        return h, 0, age + 1, weight * PATTERN_DECAY
    return (h - 1) if h > 0 else 0, 30, age + 1, weight * PATTERN_DECAY


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


def _build_result(
    adjusted_live: AdjustedPvForecast,
    now: datetime,
    *,
    current_bucket_pv_kwh_per_h: float,
    current_pv_remaining_kwh: float,
    current_cons_remaining_kwh: float,
) -> ExtrapolatedLive:
    """Assemble result for variants 1 + 2 (no future-bucket override)."""
    adjusted = _build_extrapolated_forecast(
        adjusted_live, now, current_bucket_pv_kwh_per_h=current_bucket_pv_kwh_per_h
    )
    remaining_kwh = _sum_remaining_kwh(adjusted, now)
    target_soc = PvForecast._calculate_target_soc(  # noqa: SLF001 — same-package use
        adjusted_live,
        consumption_profile=None,
        now=now,
        current_bucket_override=(current_pv_remaining_kwh, current_cons_remaining_kwh),
    )
    return ExtrapolatedLive(
        adjusted=adjusted, remaining_kwh=remaining_kwh, target_soc=target_soc
    )


def _build_extrapolated_forecast(
    forecast: AdjustedPvForecast,
    now: datetime,
    *,
    current_bucket_pv_kwh_per_h: float,
    future_pv_kwh_per_h_overrides: dict[tuple[int, int], float] | None = None,
) -> AdjustedPvForecast:
    """Build AdjustedPvForecast copy with current bucket rescaled, optionally future too.

    - Current bucket pv_estimate_adjusted := current_bucket_pv_kwh_per_h
    - Future buckets: use future_pv_kwh_per_h_overrides if provided, else unchanged
    - Past buckets: unchanged
    """
    start_hour = now.hour
    start_minute = 0 if now.minute < 30 else 30

    new_periods: list[AdjustedPeriod] = []
    total_kwh = 0.0
    for period in forecast.forecast:
        dt = datetime.fromisoformat(period.period_start)
        is_current = dt.hour == start_hour and dt.minute == start_minute
        is_future = dt.hour > start_hour or (
            dt.hour == start_hour and dt.minute > start_minute
        )
        if is_current:
            adj_rate = current_bucket_pv_kwh_per_h
        elif is_future and future_pv_kwh_per_h_overrides is not None:
            adj_rate = future_pv_kwh_per_h_overrides.get(
                (dt.hour, dt.minute), period.pv_estimate_adjusted
            )
        else:
            adj_rate = period.pv_estimate_adjusted
        new_periods.append(
            AdjustedPeriod(
                period_start=period.period_start,
                pv_estimate_adjusted=round(adj_rate, 4),
            )
        )
        total_kwh += adj_rate / 2

    return AdjustedPvForecast(forecast=new_periods, total_kwh=round(total_kwh, 4))


def _sum_remaining_kwh(forecast: AdjustedPvForecast, now: datetime) -> float:
    """Sum kWh from current bucket onwards (past excluded).

    Operates on an already-extrapolated forecast (current bucket already rescaled).
    """
    start_hour = now.hour
    start_minute = 0 if now.minute < 30 else 30
    total = 0.0
    for period in forecast.forecast:
        dt = datetime.fromisoformat(period.period_start)
        if dt.hour < start_hour or (dt.hour == start_hour and dt.minute < start_minute):
            continue
        total += period.pv_estimate_adjusted / 2
    return round(total, 4)


# Suppress unused-import noise — CONSUMPTION_PER_30MIN is referenced in docstring.
_ = CONSUMPTION_PER_30MIN
