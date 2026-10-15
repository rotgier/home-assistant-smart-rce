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
import math
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

    def __init__(self, live: ForecastStrategy | None = None) -> None:
        """`live`: the LIVE strategy to extrapolate; None = production's enum member.

        A replay passes its own LIVE instance, so replaying never reads (or
        disturbs) the strategies production dispatches to.
        """
        super().__init__()
        self._live = live

    def _compute(self, ctx: ForecastContext) -> PvForecastResult | None:
        pv_forecast_live = self._live_result()
        if pv_forecast_live is None or not ctx.solcast_today:
            return None
        return self._run_extrapolation(ctx, pv_forecast_live)

    def _live_result(self) -> PvForecastResult | None:
        if self._live is not None:
            return self._live.result
        # Lazy import to break the strategies → forecast_enum → strategies cycle.
        from .forecast_enum import PvForecast

        return PvForecast.LIVE.result

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

    pretty_label = "Extrap Pattern"

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

    pretty_label = "Extrap Proportional"

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
        future_overrides = self._future_overrides(
            solcast_by_bucket, ctx.now, cum_s, pv_forecast_live
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

    def _future_overrides(
        self,
        solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
        now: datetime,
        cum_s: float,
        pv_forecast_live: PvForecastResult,  # noqa: ARG002 — used by ExtrapBlend
    ) -> dict[tuple[int, int], float]:
        """Future rates for this variant — subclasses bound or blend them."""
        return ExtrapProportionalStrategy._project_future_buckets(
            solcast_by_bucket=solcast_by_bucket, now=now, cum_s=cum_s
        )


class ExtrapProportionalCappedStrategy(ExtrapProportionalStrategy):
    """Proportional held inside Solcast's band: `p10 <= rate <= p50 + 0.3 (p90-p50)`.

    Proportional's score has no upper bound, so a bright morning against a
    small median multiplies the whole midday; and a dull start pushes it below
    p10. The floor helps more than the cap (backtest 3-12.10.2026,
    `research/2026-10-11-backtest-poranne-ladowanie/extrap_accuracy.py`: mean
    error of 10-16 PV from 09:10 0.13 -> 0.10).
    """

    pretty_label = "Extrap Proportional Capped"

    # Share of the way from p50 to p90 the projection may reach.
    _UPPER_SHARE: Final[float] = 0.3

    def _future_overrides(
        self,
        solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
        now: datetime,
        cum_s: float,
        pv_forecast_live: PvForecastResult,
    ) -> dict[tuple[int, int], float]:
        raw = super()._future_overrides(solcast_by_bucket, now, cum_s, pv_forecast_live)
        return {
            bucket: ExtrapProportionalCappedStrategy._bound(
                rate, solcast_by_bucket[bucket]
            )
            for bucket, rate in raw.items()
        }

    @staticmethod
    def _bound(rate: float, sp: SolcastPeriod) -> float:
        """Clamp a projected rate to [p10, p50 + share x (p90 - p50)]."""
        upper = sp.pv_estimate + ExtrapProportionalCappedStrategy._UPPER_SHARE * (
            sp.pv_estimate90 - sp.pv_estimate
        )
        return max(0.0, min(max(rate, sp.pv_estimate10), upper))


class ExtrapBlendStrategy(ExtrapProportionalCappedStrategy):
    """Capped Proportional near now, fading into Live further ahead.

    What PV does now foretells the next hour well and the afternoon poorly; the
    weather-adjusted Live is the better guess far ahead. Rate per future bucket:

        live + w(dt) x (capped - live),
        w = 1 within the first hour, then 0.5 ** ((dt - 1 h) / 2.5 h)

    with dt from now to the bucket's middle. Backtest 3-12.10.2026
    (`horizon_decay.py`): mean error of 10-16 PV 0.111 vs 0.133 without the
    fade and 0.167 for Live alone; a linear fade to zero at 6-8 h scores the
    same.
    """

    pretty_label = "Extrap Blend"

    _FULL_TRUST_H: Final[float] = 1.0
    _HALF_LIFE_H: Final[float] = 2.5

    def _future_overrides(
        self,
        solcast_by_bucket: dict[tuple[int, int], SolcastPeriod],
        now: datetime,
        cum_s: float,
        pv_forecast_live: PvForecastResult,
    ) -> dict[tuple[int, int], float]:
        capped = super()._future_overrides(
            solcast_by_bucket, now, cum_s, pv_forecast_live
        )
        live = ExtrapBlendStrategy._live_by_bucket(pv_forecast_live, now)
        out: dict[tuple[int, int], float] = {}
        for (h, m), rate in capped.items():
            live_rate = live.get((h, m))
            if live_rate is None:
                out[(h, m)] = rate
                continue
            middle = now.replace(hour=h, minute=m, second=0, microsecond=0)
            hours_ahead = ((middle - now).total_seconds() + 15 * 60) / 3600
            weight = ExtrapBlendStrategy._weight(hours_ahead)
            out[(h, m)] = max(0.0, live_rate + weight * (rate - live_rate))
        return out

    @staticmethod
    def _live_by_bucket(
        pv_forecast_live: PvForecastResult, now: datetime
    ) -> dict[tuple[int, int], float]:
        out: dict[tuple[int, int], float] = {}
        for period in pv_forecast_live.forecast:
            start = datetime.fromisoformat(str(period.period_start).replace(" ", "T"))
            if start.date() == now.date():
                out[(start.hour, start.minute)] = period.pv_estimate_adjusted
        return out

    @staticmethod
    def _weight(hours_ahead: float) -> float:
        """Trust in the extrapolation `hours_ahead` from now."""
        if hours_ahead <= ExtrapBlendStrategy._FULL_TRUST_H:
            return 1.0
        return math.pow(
            0.5,
            (hours_ahead - ExtrapBlendStrategy._FULL_TRUST_H)
            / ExtrapBlendStrategy._HALF_LIFE_H,
        )


class ExtrapBandStrategy(_ExtrapStrategyBase):
    """2-zone band-clamped score anchored at [p10, p90].

    Future rate = `p10 + cumS × (p90 - p10)`; clamped above p90 and at
    zero below `p10 × (1 + cumS)`.
    """

    pretty_label = "Extrap Band"

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

    pretty_label = "Extrap Band Recent"

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
