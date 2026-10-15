"""Variant results per snapshot — what every scored PV forecast said at that moment.

A decision consumed the latest snapshot taken before it (`consumed`), so the
score reads the variants' results of that snapshot. Two ways to get them:

- `from_archive` (Home Assistant, nightly scoring): production variants as
  production computed them, straight from the archived `variants`; only what
  the archive lacks is computed — Live Soft with other alphas, raw Solcast
  p50, and the weather variants on days rebuilt from the recorder before they
  existed. Those are context-only strategies, so fresh instances fed every
  snapshot of the day in order give what production would have shown.
  Extrapolations are never recomputed here: they read LIVE, and the enum
  singletons belong to production.
- `replay` (offline tools): every variant recomputed on fresh instances,
  extrapolations wired to the replay's own LIVE — a new variant in code gets
  a score on the whole archive.

Strategies keep their previous result when an input is missing, so both run
a day from a clean state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from ..pv_forecast import (
    AdjustedPeriod,
    ForecastStrategy,
    LiveSoftStrategy,
    PvForecast,
    PvForecastResult,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from ..pv_forecast import ForecastContext
    from ..pv_forecast.forecast_snapshot import ForecastSnapshot

# Production steers by Live Soft (alpha 0.4 since 15.10.2026). Live Soft is
# always computed here — the archive holds it with the alpha of its day — so a
# ranking row never mixes alphas; `production_setup` picks the day's variant.
PRODUCTION_VARIANT: Final = PvForecast.LIVE_SOFT.key
RAW_SOLCAST: Final = "solcast_p50"
EXTRA_ALPHAS: Final = (0.3, 0.5, 0.6)

# Scored variants, in display order.
SCORED_VARIANTS: Final[tuple[str, ...]] = (
    RAW_SOLCAST,
    PvForecast.AT_6.key,
    PvForecast.LIVE.key,
    "live_soft_0.3",
    PRODUCTION_VARIANT,
    "live_soft_0.5",
    "live_soft_0.6",
    *(variant.key for variant in PvForecast.extrap()),
)

# A decision reads a snapshot at most this old; older means the archive has a hole.
CONSUMED_MAX_AGE: Final = timedelta(minutes=40)


@dataclass(frozen=True)
class ScoredSnapshot:
    """One snapshot with the result of every scored variant (None = no result)."""

    taken_at: datetime
    context: ForecastContext
    results: Mapping[str, PvForecastResult | None]
    # "live" or "recorder_backfill" — copied from the snapshot.
    source: str = "live"

    def result(self, variant: str) -> PvForecastResult | None:
        return self.results.get(variant)


def consumed(
    snapshots: Sequence[ScoredSnapshot],
    decision: datetime,
    max_age: timedelta = CONSUMED_MAX_AGE,
) -> ScoredSnapshot | None:
    """Return the snapshot a decision at `decision` used: the latest before it."""
    before = [s for s in snapshots if decision - max_age <= s.taken_at <= decision]
    return before[-1] if before else None


def from_archive(snapshots: Iterable[ForecastSnapshot]) -> list[ScoredSnapshot]:
    """Read archived results, adding the context-only variants it lacks."""
    extras = _ExtraStrategies()
    out: list[ScoredSnapshot] = []
    for snapshot in snapshots:
        computed = extras.update(snapshot.context)
        archived = {key: outcome.result for key, outcome in snapshot.variants.items()}
        results = {key: computed.get(key) for key in _ExtraStrategies.ALWAYS}
        for key in SCORED_VARIANTS:
            if key in results:
                continue
            results[key] = (
                archived[key] if archived.get(key) is not None else computed.get(key)
            )
        out.append(
            ScoredSnapshot(
                snapshot.taken_at, snapshot.context, results, snapshot.source
            )
        )
    return out


def replay(snapshots: Iterable[ForecastSnapshot]) -> list[ScoredSnapshot]:
    """Every variant recomputed from the archived contexts on fresh instances."""
    live = PvForecast.LIVE
    fresh_live = _fresh(live.strategy, None)
    strategies: dict[str, ForecastStrategy] = {
        variant.key: (
            fresh_live if variant is live else _fresh(variant.strategy, fresh_live)
        )
        for variant in PvForecast  # enum order = production dispatch order
    }
    extras = {f"live_soft_{alpha}": _soft(alpha) for alpha in EXTRA_ALPHAS}
    out: list[ScoredSnapshot] = []
    for snapshot in snapshots:
        ctx = snapshot.context
        for strategy in (*strategies.values(), *extras.values()):
            strategy.update(ctx)
        results: dict[str, PvForecastResult | None] = {
            key: strategy.result for key, strategy in strategies.items()
        }
        results |= {key: strategy.result for key, strategy in extras.items()}
        results[RAW_SOLCAST] = raw_solcast(ctx)
        out.append(ScoredSnapshot(snapshot.taken_at, ctx, results, snapshot.source))
    return out


def raw_solcast(ctx: ForecastContext) -> PvForecastResult | None:
    """Solcast's median as it came, no weather adjustment."""
    if not ctx.solcast_today:
        return None
    periods = [AdjustedPeriod(p.period_start, p.pv_estimate) for p in ctx.solcast_today]
    return PvForecastResult(periods, sum(p.pv_estimate for p in ctx.solcast_today) / 2)


class _ExtraStrategies:
    """Context-only strategies run alongside the archive, from a clean state."""

    # Always computed: not in the archive, or (Live Soft) archived with the
    # alpha of its day.
    ALWAYS: Final = (
        PRODUCTION_VARIANT,
        *(f"live_soft_{a}" for a in EXTRA_ALPHAS),
        RAW_SOLCAST,
    )

    def __init__(self) -> None:
        self._strategies: dict[str, ForecastStrategy] = {
            variant.key: _fresh(variant.strategy, None)
            for variant in (PvForecast.AT_6, PvForecast.LIVE, PvForecast.LIVE_SOFT)
        }
        self._strategies |= {f"live_soft_{a}": _soft(a) for a in EXTRA_ALPHAS}

    def update(self, ctx: ForecastContext) -> dict[str, PvForecastResult | None]:
        for strategy in self._strategies.values():
            strategy.update(ctx)
        results = {key: s.result for key, s in self._strategies.items()}
        results[RAW_SOLCAST] = raw_solcast(ctx)
        return results


def _fresh(
    strategy: ForecastStrategy, live: ForecastStrategy | None
) -> ForecastStrategy:
    """Create a new instance of `strategy`'s class: same axis, no result yet."""
    cls = type(strategy)
    if strategy.is_extrap:
        return cls(live=live)  # type: ignore[call-arg]
    return cls(today=strategy.is_today)  # type: ignore[call-arg]


def _soft(alpha: float) -> ForecastStrategy:
    """Live Soft with another alpha — a variant production does not run."""
    cls = type(f"LiveSoft{alpha}", (LiveSoftStrategy,), {"_ALPHA": alpha})
    strategy: ForecastStrategy = cls(today=True)
    return strategy
