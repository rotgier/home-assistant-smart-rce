"""Extrapolation-based PV forecast strategies.

Each subclass of `_ExtrapStrategyBase` runs the shared
`weighted_score_over_buckets` core with its own `score_fn` (4-zone,
proportional, band, band-recent) and projects future buckets per its
own algorithm. Cross-cutting helpers (assemble, index_solcast_by_bucket,
weighted_score_over_buckets) live in `extrapolation_utils.py`;
per-variant algorithm details are `@staticmethod` on each class below.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from .extrapolation_utils import (
    assemble,
    index_solcast_by_bucket,
    weighted_score_over_buckets,
)
from .strategy_base import (
    ForecastContext,
    ForecastStrategy,
    PvForecastResult,
    SolcastPeriod,
)

# Minimum rate gap (kWh/h) for ratio division to be considered stable.
# Below this, score formulas fall back to a wider quantile or return None.
_RATE_EPS: float = 0.05


class _ExtrapStrategyBase(ForecastStrategy):
    """Base for EXTRAP variants — extrapolates LIVE on realized PV history.

    `assemble` in `extrapolation_utils` already handles in-progress
    patch + future overrides → `supports_in_progress_patch=False`.
    All EXTRAP variants are today-axis (`is_today=True` inherited from base).
    """

    supports_in_progress_patch = False
    is_extrap = True

    def _compute(self, ctx: ForecastContext) -> PvForecastResult | None:
        # Lazy import to break the strategies → forecast_enum → strategies cycle.
        from .forecast_enum import PvForecast

        pv_forecast_live = PvForecast.LIVE.result
        if pv_forecast_live is None or not ctx.solcast_today:
            return None
        return self._run_extrapolation(ctx, pv_forecast_live)

    def _run_extrapolation(
        self, ctx: ForecastContext, pv_forecast_live: PvForecastResult
    ) -> PvForecastResult | None:
        """Subclass: build the EXTRAP result from its own algorithm."""
        raise NotImplementedError


class ExtrapPatternStrategy(_ExtrapStrategyBase):
    """4-zone weighted realization-score pattern (calibrated).

    Each past + current bucket gets a normalized score on a 4-zone scale
    using Solcast's three quantiles (p10, estimate=p50, p90):

        S < 0     : realized < p10        S = realized/p10 - 1   (range -1..0)
        S in 0..1 : p10 ≤ realized ≤ est  S = (real-p10)/(est-p10)
        S in 1..2 : est < realized ≤ p90  S = 1 + (real-est)/(p90-est)
        S > 2     : realized > p90        S = 2 + (real-p90)/p90

    Weighted average (current weight 1.0, each step back ×PATTERN_DECAY)
    is mapped back through the inverse of the same 4-zone scale to
    project each future bucket's PV rate.
    """

    pretty_label = "Live Extrapolated Pattern"

    def _run_extrapolation(
        self, ctx: ForecastContext, pv_forecast_live: PvForecastResult
    ) -> PvForecastResult | None:
        if ctx.signals.bucket_so_far_kwh is None or ctx.signals.pv_power_w is None:
            return None
        solcast_by_bucket = index_solcast_by_bucket(ctx.solcast_today, ctx.now)
        score = weighted_score_over_buckets(
            solcast_by_bucket=solcast_by_bucket,
            now=ctx.now,
            realized_pv_today=ctx.realized_pv_today,
            pv_bucket_so_far_kwh=ctx.signals.bucket_so_far_kwh,
            pv_power_w_5min=ctx.signals.pv_power_w,
            score_fn=lambda real, sp: ExtrapPatternStrategy._compute_score(
                real, sp.pv_estimate10, sp.pv_estimate, sp.pv_estimate90
            ),
            max_age=24,
        )
        if score is None:
            return None
        future_overrides = ExtrapPatternStrategy._project_future_buckets(
            solcast_by_bucket=solcast_by_bucket, now=ctx.now, score=score
        )
        return assemble(
            pv_forecast_live=pv_forecast_live,
            now=ctx.now,
            pv_bucket_so_far_kwh=ctx.signals.bucket_so_far_kwh,
            pv_power_w_5min=ctx.signals.pv_power_w,
            future_overrides=future_overrides,
        )

    @staticmethod
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

    @staticmethod
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
            projected_rate = ExtrapPatternStrategy._project_rate_from_score(
                p10_rate=sp.pv_estimate10,
                est_rate=sp.pv_estimate,
                p90_rate=sp.pv_estimate90,
                score=score,
            )
            overrides[(h, m)] = max(0.0, projected_rate)
        return overrides

    @staticmethod
    def _project_rate_from_score(
        p10_rate: float,
        est_rate: float,
        p90_rate: float,
        score: float,
    ) -> float:
        """Inverse of `_compute_score` — given score, project PV rate (kWh/h)."""
        if score < 0.0:
            return max(0.0, p10_rate * (1.0 + score))
        if score <= 1.0:
            return p10_rate + score * (est_rate - p10_rate)
        if score <= 2.0:
            if (p90_rate - est_rate) >= _RATE_EPS:
                return est_rate + (score - 1.0) * (p90_rate - est_rate)
            return est_rate * (1.0 + (score - 1.0))
        return p90_rate * (1.0 + (score - 2.0))


class ExtrapProportionalStrategy(_ExtrapStrategyBase):
    """Proportional median — band-width independent `(real-est)/est` score.

    Future rate = `est × (1 + cumS)`, floored at cumS = `_PROPORTIONAL_FLOOR`
    so projection stays positive when realization runs well below median.
    """

    pretty_label = "Live Extrapolated Proportional"

    # Clamp for negative cumS in projection — prevent project=0 when cumS=-1.
    _PROPORTIONAL_FLOOR: Final[float] = -0.95

    def _run_extrapolation(
        self, ctx: ForecastContext, pv_forecast_live: PvForecastResult
    ) -> PvForecastResult | None:
        if ctx.signals.bucket_so_far_kwh is None or ctx.signals.pv_power_w is None:
            return None
        solcast_by_bucket = index_solcast_by_bucket(ctx.solcast_today, ctx.now)
        cum_s = weighted_score_over_buckets(
            solcast_by_bucket=solcast_by_bucket,
            now=ctx.now,
            realized_pv_today=ctx.realized_pv_today,
            pv_bucket_so_far_kwh=ctx.signals.bucket_so_far_kwh,
            pv_power_w_5min=ctx.signals.pv_power_w,
            score_fn=lambda real, sp: ExtrapProportionalStrategy._compute_score(
                real, sp.pv_estimate
            ),
            max_age=24,
        )
        if cum_s is None:
            return None
        future_overrides = ExtrapProportionalStrategy._project_future_buckets(
            solcast_by_bucket=solcast_by_bucket, now=ctx.now, cum_s=cum_s
        )
        return assemble(
            pv_forecast_live=pv_forecast_live,
            now=ctx.now,
            pv_bucket_so_far_kwh=ctx.signals.bucket_so_far_kwh,
            pv_power_w_5min=ctx.signals.pv_power_w,
            future_overrides=future_overrides,
        )

    @staticmethod
    def _compute_score(realized_rate: float, est_rate: float) -> float | None:
        """Score = (realized - est) / est. Centered at 0 (S=0 → real=est)."""
        if est_rate < _RATE_EPS:
            return None
        return (realized_rate - est_rate) / est_rate

    @staticmethod
    def _project_future_buckets(
        solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
        now: datetime,
        cum_s: float,
    ) -> dict[tuple[int, int], float]:
        """For each future bucket, project rate = est × (1 + cumS)."""
        current_hour = now.hour
        current_minute = 0 if now.minute < 30 else 30
        clamped = max(cum_s, ExtrapProportionalStrategy._PROPORTIONAL_FLOOR)
        overrides: dict[tuple[int, int], float] = {}
        for (h, m), sp in solcast_by_bucket.items():
            if h < current_hour or (h == current_hour and m <= current_minute):
                continue
            overrides[(h, m)] = max(0.0, sp.pv_estimate * (1.0 + clamped))
        return overrides


class ExtrapBandStrategy(_ExtrapStrategyBase):
    """2-zone band-clamped score anchored at [p10, p90].

    Future rate = `p10 + cumS × (p90 - p10)`; clamped above p90 and at
    zero below `p10 × (1 + cumS)`.
    """

    pretty_label = "Live Extrapolated Band"

    def _run_extrapolation(
        self, ctx: ForecastContext, pv_forecast_live: PvForecastResult
    ) -> PvForecastResult | None:
        if ctx.signals.bucket_so_far_kwh is None or ctx.signals.pv_power_w is None:
            return None
        solcast_by_bucket = index_solcast_by_bucket(ctx.solcast_today, ctx.now)
        cum_s = weighted_score_over_buckets(
            solcast_by_bucket=solcast_by_bucket,
            now=ctx.now,
            realized_pv_today=ctx.realized_pv_today,
            pv_bucket_so_far_kwh=ctx.signals.bucket_so_far_kwh,
            pv_power_w_5min=ctx.signals.pv_power_w,
            score_fn=lambda real, sp: ExtrapBandStrategy._compute_score(
                real, sp.pv_estimate10, sp.pv_estimate90
            ),
            max_age=24,
        )
        if cum_s is None:
            return None
        future_overrides = ExtrapBandStrategy._project_future_buckets(
            solcast_by_bucket=solcast_by_bucket, now=ctx.now, cum_s=cum_s
        )
        return assemble(
            pv_forecast_live=pv_forecast_live,
            now=ctx.now,
            pv_bucket_so_far_kwh=ctx.signals.bucket_so_far_kwh,
            pv_power_w_5min=ctx.signals.pv_power_w,
            future_overrides=future_overrides,
        )

    @staticmethod
    def _compute_score(
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

    @staticmethod
    def _project_future_buckets(
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


class ExtrapBandRecentStrategy(ExtrapBandStrategy):
    """Band-clamped with narrowed recent-only lookback.

    Same band-clamped score + projection as `ExtrapBandStrategy` but
    limited to `_BAND_RECENT_MAX_AGE=1` — only the current bucket and
    the immediately prior bucket contribute. Captures short-horizon
    weather trend without carrying morning bias into afternoon
    projections. Inherits `_compute_score` + `_project_future_buckets`;
    only the scoring `max_age` differs.
    """

    pretty_label = "Live Extrapolated Band Recent"

    # Max age (steps back from current) — only current + 1 prior bucket
    # contribute. Captures recent trend without morning bias.
    _BAND_RECENT_MAX_AGE: Final[int] = 1

    def _run_extrapolation(
        self, ctx: ForecastContext, pv_forecast_live: PvForecastResult
    ) -> PvForecastResult | None:
        if ctx.signals.bucket_so_far_kwh is None or ctx.signals.pv_power_w is None:
            return None
        solcast_by_bucket = index_solcast_by_bucket(ctx.solcast_today, ctx.now)
        cum_s = weighted_score_over_buckets(
            solcast_by_bucket=solcast_by_bucket,
            now=ctx.now,
            realized_pv_today=ctx.realized_pv_today,
            pv_bucket_so_far_kwh=ctx.signals.bucket_so_far_kwh,
            pv_power_w_5min=ctx.signals.pv_power_w,
            score_fn=lambda real, sp: self._compute_score(
                real, sp.pv_estimate10, sp.pv_estimate90
            ),
            max_age=self._BAND_RECENT_MAX_AGE,
        )
        if cum_s is None:
            return None
        future_overrides = self._project_future_buckets(
            solcast_by_bucket=solcast_by_bucket, now=ctx.now, cum_s=cum_s
        )
        return assemble(
            pv_forecast_live=pv_forecast_live,
            now=ctx.now,
            pv_bucket_so_far_kwh=ctx.signals.bucket_so_far_kwh,
            pv_power_w_5min=ctx.signals.pv_power_w,
            future_overrides=future_overrides,
        )
