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

In-progress bucket projection is encoded into the strategy's `PvProfile`
(in-progress bucket value = "kWh remaining from now") and fed to
`calculate_target_soc`. Future + past buckets keep their full forecast
values. Consumption is constant `ConsumptionProfile.flat()` baseline —
in-progress cons time-prorate is restored via `live_consumption_w` in C4.
"""

from __future__ import annotations

from datetime import datetime

from .pv_forecast import (
    CONSUMPTION_PER_30MIN,
    AdjustedPeriod,
    AdjustedPvForecast,
    ConsumptionProfile,
    ExtrapolatedLive,
    PvForecast,
    PvProfile,
    SolcastPeriod,
)

# Constant-baseline profile reused by every extrapolated variant —
# matches the target_soc_live baseline. Until C4, the in-progress bucket
# cons is also taken from this flat profile (slight overestimate of
# remaining-bucket cons; documented regression).
_DEFAULT_CONS_PROFILE = ConsumptionProfile.flat()


def _bucket_key(now: datetime) -> tuple[int, int]:
    """Round `now` down to the enclosing 30-min bucket key."""
    return now.hour, 0 if now.minute < 30 else 30


def _with_current_bucket(profile: PvProfile, now: datetime, value: float) -> PvProfile:
    """Replace the in-progress bucket with `value` (kWh remaining from now).

    Other buckets keep their full-bucket values from the source profile.
    The asymmetry (current=remaining, future=full) is the same model
    `current_bucket_override` had before C2 — encoded as data now.
    """
    return PvProfile(buckets={**profile.buckets, _bucket_key(now): value})


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
    start_charge_hour: int | None = None,
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
    return _build_result(
        adjusted_live,
        now,
        current_bucket_pv_kwh_per_h=current_pv_rate,
        current_pv_remaining_kwh=current_pv_remaining_kwh,
        start_charge_hour=start_charge_hour,
    )


def extrapolate_5min_rate(
    adjusted_live: AdjustedPvForecast,
    now: datetime,
    pv_power_w: float | None,
    consumption_w: float | None,
    pv_bucket_so_far_kwh: float | None = None,
    consumption_bucket_so_far_kwh: float | None = None,
    start_charge_hour: int | None = None,
) -> ExtrapolatedLive:
    """Variant 2 — keep realized so-far, project remaining with 5-min power.

    Bucket reconstruction:
    - past portion of bucket: actual realized kWh from utility meter
      (`pv_bucket_so_far_kwh`) — what already happened, don't overwrite
    - remaining portion: extrapolated as `pv_power_w/1000 × remaining_min/60`
      using current 5-min average power (more responsive than utility-meter
      prorate when conditions are changing — cloud roll-in/-out)

    Full bucket equivalent rate (for chart display) = (so_far + remaining) × 2.

    Fallback when so-far values are unavailable / too early in bucket:
    treat full bucket rate = current 5-min power directly (= original
    behavior). This preserves the variant's value at bucket start.
    """
    if pv_power_w is None or consumption_w is None:
        return ExtrapolatedLive.empty()

    elapsed_min = now.minute % 30
    remaining_min = 30 - elapsed_min

    # Remaining contribution (kWh) injected into the strategy's PvProfile for
    # the in-progress bucket — energy from NOW to bucket end.
    current_pv_remaining_kwh = pv_power_w / 1000 * remaining_min / 60

    # Full bucket rate (kWh/h, for chart display). Combine realized so-far with
    # remaining extrapolation when so-far is available + we're past the noise
    # threshold; otherwise fall back to pure 5-min power rate.
    if (
        pv_bucket_so_far_kwh is not None
        and elapsed_min >= MIN_ELAPSED_FOR_REALIZED_PRORATE
    ):
        full_bucket_pv_kwh = pv_bucket_so_far_kwh + current_pv_remaining_kwh
        pv_rate_kwh_per_h = full_bucket_pv_kwh * 2  # 30-min bucket → kWh/h
    else:
        pv_rate_kwh_per_h = pv_power_w / 1000  # fallback (W → kW = kWh/h)

    return _build_result(
        adjusted_live,
        now,
        current_bucket_pv_kwh_per_h=pv_rate_kwh_per_h,
        current_pv_remaining_kwh=current_pv_remaining_kwh,
        start_charge_hour=start_charge_hour,
    )


def extrapolate_calibrated_pattern(
    adjusted_live: AdjustedPvForecast,
    solcast_live: list[SolcastPeriod],
    now: datetime,
    pv_bucket_so_far_kwh: float | None,
    consumption_bucket_so_far_kwh: float | None,
    realized_pv_today: dict[tuple[int, int], float],
    start_charge_hour: int | None = None,
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
        _with_current_bucket(
            adjusted.to_profile(now.date()), now, current_pv_remaining_kwh
        ),
        consumption_profile=_DEFAULT_CONS_PROFILE,
        now=now,
        start_charge_hour=start_charge_hour,
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


def _compute_proportional_score(realized_rate: float, est_rate: float) -> float | None:
    """Score = (realized - est) / est. Centered at 0 (S=0 → real=est).

    p10/p90 not used — independent of forecast band width. Symmetric: same
    magnitude for +10% above median as -10% below. Returns None when est too
    small for stable ratio (RATE_EPS guard).
    """
    if est_rate < _RATE_EPS:
        return None
    return (realized_rate - est_rate) / est_rate


# Clamp for negative cumS in projection — prevent project=0 when cumS=-1.
# At cumS=-0.95 projection = est × 0.05; at -1 → 0 (only fully overcast yields 0).
_PROPORTIONAL_FLOOR: float = -0.95


def _compute_weighted_proportional_score(
    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
    now: datetime,
    realized_pv_today: dict[tuple[int, int], float],
    pv_bucket_so_far_kwh: float,
    elapsed_min: int,
) -> float | None:
    """Weighted (decay 0.7) average of proportional scores over today's buckets.

    Mirrors _compute_weighted_score structure but uses proportional formula
    `S = (real - est) / est` for each bucket — independent of band width.
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
            if age > 24:
                break
            continue
        if (h, m) == (current_hour, current_minute):
            realized_rate = pv_bucket_so_far_kwh * 60 / elapsed_min
        else:
            realized_kwh = realized_pv_today.get((h, m))
            if realized_kwh is None:
                h, m, age, weight = _step_back(h, m, age, weight)
                if age > 24:
                    break
                continue
            realized_rate = realized_kwh * 2
        score = _compute_proportional_score(realized_rate, sp.pv_estimate)
        if score is not None:
            scores.append((score, weight))
        h, m, age, weight = _step_back(h, m, age, weight)
        if age > 24:
            break

    if not scores:
        return None
    total_weight = sum(w for _, w in scores)
    return sum(s * w for s, w in scores) / total_weight


def _project_future_buckets_proportional(
    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
    now: datetime,
    cum_s: float,
) -> dict[tuple[int, int], float]:
    """For each future bucket, project rate = est × (1 + cumS).

    cumS clamped at _PROPORTIONAL_FLOOR (-0.95) so a deeply-bad-day score
    doesn't drive projections to 0 — leaves a 5% residual.
    """
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
    adjusted_live: AdjustedPvForecast,
    solcast_live: list[SolcastPeriod],
    now: datetime,
    pv_bucket_so_far_kwh: float | None,
    consumption_bucket_so_far_kwh: float | None,
    realized_pv_today: dict[tuple[int, int], float],
    start_charge_hour: int | None = None,
) -> ExtrapolatedLive:
    """Variant 4 — proportional-to-median realization scaling.

    Same shape as `extrapolate_calibrated_pattern` (variant 3) but uses a
    different score formula:

        S = (realized - est) / est        (centered at 0, no p10/p90 dependence)

    Weighted average (current=1.0, each step back × PATTERN_DECAY) gives cumS.
    Each future bucket projects rate = `est × (1 + cumS)`, clamped at 0.

    Pros vs variant 3:
    - Independent of forecast band width — no spike when p90-est is tiny
      (which happens in narrow-confidence buckets, especially early morning).
    - Symmetric: same magnitude for over- and under-prediction relative to est.

    Cons:
    - Ignores p10/p90 confidence — wide band days treated same as narrow.
    - Unbounded above; cumS=-0.95 floor protects projection > 0.
    """
    elapsed_min = now.minute % 30
    remaining_min = 30 - elapsed_min
    if (
        elapsed_min < MIN_ELAPSED_FOR_REALIZED_PRORATE
        or pv_bucket_so_far_kwh is None
        or consumption_bucket_so_far_kwh is None
    ):
        return ExtrapolatedLive.empty()

    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod] = {}
    for sp in solcast_live:
        dt = datetime.fromisoformat(sp.period_start)
        if dt.date() != now.date():
            continue
        solcast_by_bucket[(dt.hour, dt.minute)] = sp

    cum_s = _compute_weighted_proportional_score(
        solcast_by_bucket=solcast_by_bucket,
        now=now,
        realized_pv_today=realized_pv_today,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        elapsed_min=elapsed_min,
    )
    if cum_s is None:
        return ExtrapolatedLive.empty()

    current_pv_rate = pv_bucket_so_far_kwh * 60 / elapsed_min
    current_pv_remaining_kwh = pv_bucket_so_far_kwh * remaining_min / elapsed_min

    future_overrides = _project_future_buckets_proportional(
        solcast_by_bucket=solcast_by_bucket, now=now, cum_s=cum_s
    )
    adjusted = _build_extrapolated_forecast(
        adjusted_live,
        now,
        current_bucket_pv_kwh_per_h=current_pv_rate,
        future_pv_kwh_per_h_overrides=future_overrides,
    )
    remaining_kwh = _sum_remaining_kwh(adjusted, now)
    target_soc = PvForecast._calculate_target_soc(  # noqa: SLF001 — same-package use
        _with_current_bucket(
            adjusted.to_profile(now.date()), now, current_pv_remaining_kwh
        ),
        consumption_profile=_DEFAULT_CONS_PROFILE,
        now=now,
        start_charge_hour=start_charge_hour,
    )
    return ExtrapolatedLive(
        adjusted=adjusted, remaining_kwh=remaining_kwh, target_soc=target_soc
    )


def _compute_band_score(
    realized_rate: float, p10_rate: float, p90_rate: float
) -> float | None:
    """Score on 2-zone scale anchored by [p10, p90] (no est/median use).

        S = -1..0  : real < p10        S = real/p10 - 1
        S = 0..1   : p10 ≤ real ≤ p90  S = (real - p10) / (p90 - p10)
        S = 1      : real > p90        (clamped — over-performance capped)

    Differs from 4-zone _compute_score: no explicit est zone; above p90 is
    clamped to 1.0 instead of using a >p90 ratio extension. Eliminates the
    "narrow band → explosive S" pathology since the only way to push S above
    1 would require real > p90 — and that's clamped.

    Below p10 still uses ratio bounded toward 0 at S=-1.
    """
    if realized_rate >= p90_rate:
        return 1.0  # clamp — over-performance capped at band ceiling
    if realized_rate >= p10_rate:
        if (p90_rate - p10_rate) >= _RATE_EPS:
            return (realized_rate - p10_rate) / (p90_rate - p10_rate)
        # Collapsed band (p10 ≈ p90) — treat as midpoint
        return 0.5
    # Below p10
    if p10_rate >= _RATE_EPS:
        return realized_rate / p10_rate - 1.0
    return None


def _compute_weighted_band_score(
    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
    now: datetime,
    realized_pv_today: dict[tuple[int, int], float],
    pv_bucket_so_far_kwh: float,
    elapsed_min: int,
) -> float | None:
    """Weighted (decay 0.7) band-clamped score over today's daylight buckets."""
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
            if age > 24:
                break
            continue
        if (h, m) == (current_hour, current_minute):
            realized_rate = pv_bucket_so_far_kwh * 60 / elapsed_min
        else:
            realized_kwh = realized_pv_today.get((h, m))
            if realized_kwh is None:
                h, m, age, weight = _step_back(h, m, age, weight)
                if age > 24:
                    break
                continue
            realized_rate = realized_kwh * 2
        score = _compute_band_score(realized_rate, sp.pv_estimate10, sp.pv_estimate90)
        if score is not None:
            scores.append((score, weight))
        h, m, age, weight = _step_back(h, m, age, weight)
        if age > 24:
            break

    if not scores:
        return None
    total_weight = sum(w for _, w in scores)
    return sum(s * w for s, w in scores) / total_weight


def _project_future_buckets_band(
    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
    now: datetime,
    cum_s: float,
) -> dict[tuple[int, int], float]:
    """For each future bucket: project = p10 + S × (p90 − p10), clamped at p10 floor.

    Inverse of _compute_band_score. Negative S scales p10 toward 0 (bounded at
    S=-1 → 0). cumS > 1 impossible by construction (clamp in score).
    """
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
            projected = p90  # defensive, shouldn't happen due to clamp
        overrides[(h, m)] = max(0.0, projected)
    return overrides


def extrapolate_band_clamped(
    adjusted_live: AdjustedPvForecast,
    solcast_live: list[SolcastPeriod],
    now: datetime,
    pv_bucket_so_far_kwh: float | None,
    consumption_bucket_so_far_kwh: float | None,
    realized_pv_today: dict[tuple[int, int], float],
    start_charge_hour: int | None = None,
) -> ExtrapolatedLive:
    """Variant 5 — 2-zone band-clamped realization scaling.

    Score formula (anchored by Solcast's p10 and p90 only, no est):

        below p10  : S = real/p10 - 1     (range -1..0)
        p10..p90   : S = (real-p10)/(p90-p10)   (range 0..1)
        above p90  : S = 1                (clamped)

    Weighted average (current=1.0, each step back × PATTERN_DECAY) gives cumS.
    Each future bucket projects via inverse: rate = p10 + cumS × (p90-p10),
    bounded below by 0 (cumS=-1).

    Pros vs 4-zone pattern:
    - Eliminates the >p90 explosion (clamp at S=1) — narrow-band buckets
      cannot push S beyond 1.
    - cumS bounded to [-1, +1] — clean interpretation.

    Pros vs proportional:
    - Uses both p10 and p90 confidence info (band-aware projection).
    - Wide-band future bucket gets a wider projection range (more uncertainty
      reflected); narrow-band gets a tighter projection.

    Cons:
    - Loses info when real > p90 (clamped — algorithm "doesn't know" how
      much we exceeded). For severely-underforecasted days this caps the
      projection at p90, possibly conservative.
    - est (median) ignored entirely.
    """
    elapsed_min = now.minute % 30
    remaining_min = 30 - elapsed_min
    if (
        elapsed_min < MIN_ELAPSED_FOR_REALIZED_PRORATE
        or pv_bucket_so_far_kwh is None
        or consumption_bucket_so_far_kwh is None
    ):
        return ExtrapolatedLive.empty()

    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod] = {}
    for sp in solcast_live:
        dt = datetime.fromisoformat(sp.period_start)
        if dt.date() != now.date():
            continue
        solcast_by_bucket[(dt.hour, dt.minute)] = sp

    cum_s = _compute_weighted_band_score(
        solcast_by_bucket=solcast_by_bucket,
        now=now,
        realized_pv_today=realized_pv_today,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        elapsed_min=elapsed_min,
    )
    if cum_s is None:
        return ExtrapolatedLive.empty()

    current_pv_rate = pv_bucket_so_far_kwh * 60 / elapsed_min
    current_pv_remaining_kwh = pv_bucket_so_far_kwh * remaining_min / elapsed_min

    future_overrides = _project_future_buckets_band(
        solcast_by_bucket=solcast_by_bucket, now=now, cum_s=cum_s
    )
    adjusted = _build_extrapolated_forecast(
        adjusted_live,
        now,
        current_bucket_pv_kwh_per_h=current_pv_rate,
        future_pv_kwh_per_h_overrides=future_overrides,
    )
    remaining_kwh = _sum_remaining_kwh(adjusted, now)
    target_soc = PvForecast._calculate_target_soc(  # noqa: SLF001 — same-package use
        _with_current_bucket(
            adjusted.to_profile(now.date()), now, current_pv_remaining_kwh
        ),
        consumption_profile=_DEFAULT_CONS_PROFILE,
        now=now,
        start_charge_hour=start_charge_hour,
    )
    return ExtrapolatedLive(
        adjusted=adjusted, remaining_kwh=remaining_kwh, target_soc=target_soc
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
    elapsed_min: int,
) -> float | None:
    """Compute band score weighted average with narrow lookback (current + 1 back)."""
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
            if age > BAND_RECENT_MAX_AGE:
                break
            continue
        if (h, m) == (current_hour, current_minute):
            realized_rate = pv_bucket_so_far_kwh * 60 / elapsed_min
        else:
            realized_kwh = realized_pv_today.get((h, m))
            if realized_kwh is None:
                h, m, age, weight = _step_back(h, m, age, weight)
                if age > BAND_RECENT_MAX_AGE:
                    break
                continue
            realized_rate = realized_kwh * 2
        score = _compute_band_score(realized_rate, sp.pv_estimate10, sp.pv_estimate90)
        if score is not None:
            scores.append((score, weight))
        h, m, age, weight = _step_back(h, m, age, weight)
        if age > BAND_RECENT_MAX_AGE:
            break

    if not scores:
        return None
    total_weight = sum(w for _, w in scores)
    return sum(s * w for s, w in scores) / total_weight


def extrapolate_band_clamped_recent(
    adjusted_live: AdjustedPvForecast,
    solcast_live: list[SolcastPeriod],
    now: datetime,
    pv_bucket_so_far_kwh: float | None,
    consumption_bucket_so_far_kwh: float | None,
    realized_pv_today: dict[tuple[int, int], float],
    start_charge_hour: int | None = None,
) -> ExtrapolatedLive:
    """Variant 6 — band-clamped scoring, narrow lookback (current + 1 back).

    Same band-clamped score formula as variant 5 (`extrapolate_band_clamped`)
    but limited to BAND_RECENT_MAX_AGE=1 — only the current bucket and the
    immediately prior bucket contribute to cumS.

    Use case: captures current weather trend without carrying morning bias
    into afternoon projections. Useful when conditions shift mid-day (e.g.
    cloudy morning → sunny afternoon).

    Trade-off vs full-history Band: more reactive but less stable.
    A single anomaly bucket dominates cumS; early-bucket noise (real <3min
    elapsed already short-circuited at entry) still propagates.
    """
    elapsed_min = now.minute % 30
    remaining_min = 30 - elapsed_min
    if (
        elapsed_min < MIN_ELAPSED_FOR_REALIZED_PRORATE
        or pv_bucket_so_far_kwh is None
        or consumption_bucket_so_far_kwh is None
    ):
        return ExtrapolatedLive.empty()

    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod] = {}
    for sp in solcast_live:
        dt = datetime.fromisoformat(sp.period_start)
        if dt.date() != now.date():
            continue
        solcast_by_bucket[(dt.hour, dt.minute)] = sp

    cum_s = _compute_weighted_band_score_recent(
        solcast_by_bucket=solcast_by_bucket,
        now=now,
        realized_pv_today=realized_pv_today,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        elapsed_min=elapsed_min,
    )
    if cum_s is None:
        return ExtrapolatedLive.empty()

    current_pv_rate = pv_bucket_so_far_kwh * 60 / elapsed_min
    current_pv_remaining_kwh = pv_bucket_so_far_kwh * remaining_min / elapsed_min

    future_overrides = _project_future_buckets_band(
        solcast_by_bucket=solcast_by_bucket, now=now, cum_s=cum_s
    )
    adjusted = _build_extrapolated_forecast(
        adjusted_live,
        now,
        current_bucket_pv_kwh_per_h=current_pv_rate,
        future_pv_kwh_per_h_overrides=future_overrides,
    )
    remaining_kwh = _sum_remaining_kwh(adjusted, now)
    target_soc = PvForecast._calculate_target_soc(  # noqa: SLF001 — same-package use
        _with_current_bucket(
            adjusted.to_profile(now.date()), now, current_pv_remaining_kwh
        ),
        consumption_profile=_DEFAULT_CONS_PROFILE,
        now=now,
        start_charge_hour=start_charge_hour,
    )
    return ExtrapolatedLive(
        adjusted=adjusted, remaining_kwh=remaining_kwh, target_soc=target_soc
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


def _build_result(
    adjusted_live: AdjustedPvForecast,
    now: datetime,
    *,
    current_bucket_pv_kwh_per_h: float,
    current_pv_remaining_kwh: float,
    start_charge_hour: int | None = None,
) -> ExtrapolatedLive:
    """Assemble result for variants 1 + 2 (no future-bucket override)."""
    adjusted = _build_extrapolated_forecast(
        adjusted_live, now, current_bucket_pv_kwh_per_h=current_bucket_pv_kwh_per_h
    )
    remaining_kwh = _sum_remaining_kwh(adjusted, now)
    target_soc = PvForecast._calculate_target_soc(  # noqa: SLF001 — same-package use
        _with_current_bucket(
            adjusted_live.to_profile(now.date()), now, current_pv_remaining_kwh
        ),
        consumption_profile=_DEFAULT_CONS_PROFILE,
        now=now,
        start_charge_hour=start_charge_hour,
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
