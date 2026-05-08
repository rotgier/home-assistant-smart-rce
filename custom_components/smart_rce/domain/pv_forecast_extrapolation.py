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
    """Variant 3 — projects realization factor from past buckets onto future.

    For each past + current bucket we compute a "realization factor":
        factor = (realized_kWh - p10_kWh) / (estimate_kWh - p10_kWh)
        factor = 0   → realized matched estimate10 (low / very cloudy)
        factor = 1   → realized matched estimate (median forecast)
        factor < 0   → below estimate10 (worse than pessimistic)
        factor > 1   → above estimate (better than median — also allowed)

    Past buckets read from `realized_pv_today` (utility meter history per
    closed bucket). Current bucket extrapolated from `pv_bucket_so_far_kwh /
    elapsed × 30`. Buckets with `pv_estimate / 2 < PATTERN_MIN_FORECAST_KWH`
    are skipped (pre-dawn / post-dusk noise).

    Weighted average (current = weight 1.0, each step back × PATTERN_DECAY)
    yields a single projected factor. For each future bucket f:
        predicted_kWh = p10_f + factor × (estimate_f - p10_f)
    The projected per-bucket rate replaces forecast PV; consumption uses the
    same prorate as variant 1 for current bucket and CONSUMPTION_PER_30MIN
    for future buckets.
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

    factor = _compute_weighted_factor(
        solcast_by_bucket=solcast_by_bucket,
        now=now,
        realized_pv_today=realized_pv_today,
        pv_bucket_so_far_kwh=pv_bucket_so_far_kwh,
        elapsed_min=elapsed_min,
    )
    if factor is None:
        return ExtrapolatedLive.empty()

    # Current bucket: same as variant 1 (realized prorate).
    current_pv_rate = pv_bucket_so_far_kwh * 60 / elapsed_min
    current_pv_remaining_kwh = pv_bucket_so_far_kwh * remaining_min / elapsed_min
    current_cons_remaining_kwh = (
        consumption_bucket_so_far_kwh * remaining_min / elapsed_min
    )

    # Future buckets: solcast estimate range × projected factor.
    future_overrides = _project_future_buckets(
        solcast_by_bucket=solcast_by_bucket, now=now, factor=factor
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


def _compute_weighted_factor(
    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
    now: datetime,
    realized_pv_today: dict[tuple[int, int], float],
    pv_bucket_so_far_kwh: float,
    elapsed_min: int,
) -> float | None:
    """Weighted realization factor over today's past + current daylight buckets."""
    current_hour = now.hour
    current_minute = 0 if now.minute < 30 else 30
    factors: list[tuple[float, float]] = []  # (factor, weight)
    age = 0  # current bucket age=0, prev=1, ...
    weight = 1.0
    # Iterate from current bucket backwards through today's buckets.
    h, m = current_hour, current_minute
    while True:
        sp = solcast_by_bucket.get((h, m))
        if sp is None:
            # No solcast data for this bucket; stop walking back.
            break
        estimate_kwh = sp.pv_estimate / 2
        p10_kwh = sp.pv_estimate10 / 2
        if estimate_kwh >= PATTERN_MIN_FORECAST_KWH and estimate_kwh > p10_kwh:
            if (h, m) == (current_hour, current_minute):
                # Current bucket — extrapolate from realized_so_far
                realized = pv_bucket_so_far_kwh * 30 / elapsed_min
            else:
                realized_val = realized_pv_today.get((h, m))
                if realized_val is None:
                    # Missing past bucket — skip but keep walking back.
                    h, m, age, weight = _step_back(h, m, age, weight)
                    continue
                realized = realized_val
            factor = (realized - p10_kwh) / (estimate_kwh - p10_kwh)
            factors.append((factor, weight))
        # Walk back regardless (so age/weight track absolute time, not just contributing buckets).
        h, m, age, weight = _step_back(h, m, age, weight)
        if age > 24:  # safety: don't iterate beyond a day
            break

    if not factors:
        return None
    total_weight = sum(w for _, w in factors)
    return sum(f * w for f, w in factors) / total_weight


def _step_back(h: int, m: int, age: int, weight: float) -> tuple[int, int, int, float]:
    """Move 30 min back; decay weight."""
    if m == 30:
        return h, 0, age + 1, weight * PATTERN_DECAY
    return (h - 1) if h > 0 else 0, 30, age + 1, weight * PATTERN_DECAY


def _project_future_buckets(
    solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
    now: datetime,
    factor: float,
) -> dict[tuple[int, int], float]:
    """For each future bucket, return projected PV kWh/h rate = p10 + factor × (est - p10).

    Uses raw solcast pv_estimate / pv_estimate10 (kWh/h hourly rate).
    """
    current_hour = now.hour
    current_minute = 0 if now.minute < 30 else 30
    overrides: dict[tuple[int, int], float] = {}
    for (h, m), sp in solcast_by_bucket.items():
        if h < current_hour or (h == current_hour and m <= current_minute):
            continue
        # Solcast pv_estimate is hourly rate — same unit as pv_estimate_adjusted.
        projected_rate = sp.pv_estimate10 + factor * (sp.pv_estimate - sp.pv_estimate10)
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
