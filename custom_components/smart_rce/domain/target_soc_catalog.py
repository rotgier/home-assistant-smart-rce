"""TargetSocCatalog — aggregate orchestrating per-variant TargetSoc entities.

DDD split from `PvForecasts`: this aggregate owns the "what battery
target SOC results from forecast + consumption" concern (per-variant
`TargetSoc` entities + consumption profiles + cons-side live signal +
pre-charge gates), while `PvForecasts` owns the "what PV looks like"
concern (forecast scenarios + extrapolation + PV-side live signals).

`recalculate_target_soc(updater, now)` iterates **all 8 PvForecast
variants** uniformly. Unbound variants (Iter 2: EXTRAP × 4) naturally
produce `None` from their `TargetSoc._one()` because `variant.result`
is `None`. EXTRAP sensors keep reading from `ExtrapolatedLive.target_soc`
until Iter 3 binds EXTRAP strategies — at which point the catalog
iteration is unchanged and sensors just switch source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .consumption_profiles import ConsumptionProfile, ConsumptionProfiles
from .pv_forecast_strategy import PvForecast
from .target_soc import TargetSoc, TargetSocContext, TargetSocInputs

if TYPE_CHECKING:
    from .pv_forecasts import PvForecasts


@dataclass
class TargetSocCatalog:
    """Per-variant target_soc orchestrator.

    Holds 8 `TargetSoc` entities (one per `PvForecast` variant) plus
    consumption profiles (rich entity with refresh lifecycle) and
    cons-side inputs (live consumption + pre-charge gates). On each
    `recalculate_target_soc` call iterates entities uniformly, feeding
    each its date-axis ctx + consumption profile list.
    """

    _inputs: TargetSocInputs = field(default_factory=TargetSocInputs)
    consumption_profiles: ConsumptionProfiles = field(
        default_factory=lambda: ConsumptionProfiles.empty()
    )
    target_socs: dict[PvForecast, TargetSoc] = field(
        default_factory=lambda: {v: TargetSoc(variant=v) for v in PvForecast}
    )

    # — Read accessor —

    @property
    def inputs(self) -> TargetSocInputs:
        """Read-only snapshot of cons-side live + pre-charge gates."""
        return self._inputs

    # — Update methods (Tell-Don't-Ask) —

    def refresh_inputs(self, inputs: TargetSocInputs) -> None:
        """Atomic snapshot of cons-side live + pre-charge gates."""
        self._inputs = inputs

    def recalculate_target_soc(self, updater: PvForecasts, now: datetime) -> None:
        """Recompute every `TargetSoc` entity from current forecasts + profiles.

        Today variants build now-aware profiles via
        `PvForecastResult.to_profile(today, now, pv_power_w_5min=...)`
        and `ConsumptionProfile.to_view(now, live_consumption_w=...)`.
        When either live signal is missing, today variants stay `None`
        (fail-hard contract — no stale forecast-prorate fallback).

        Tomorrow variants pass `now=None` (full-window deficit, no live
        in-progress concept since current power doesn't carry across
        days), so live signals are not needed.

        Pre-charge inter-hour clamp via `start_charge_hour_{today,tomorrow}`
        applies symmetrically: a sunny pre-charge hour cannot mask a later
        deficit by propagating its positive cumulative balance across the
        hour boundary into the gated post-charge window.
        """
        today_ctx = TargetSocContext(
            target_date=now.date(),
            signals=updater.signals,
            live_consumption_w=self._inputs.live_consumption_w,
            start_charge_hour=self._inputs.start_charge_hour_today,
            now=now,
        )
        tomorrow_ctx = TargetSocContext(
            target_date=now.date() + timedelta(days=1),
            signals=updater.signals,
            live_consumption_w=None,  # not used in is_today=False branch
            start_charge_hour=self._inputs.start_charge_hour_tomorrow,
            now=now,
        )
        flat_cons = ConsumptionProfile.flat()
        for entity in self.target_socs.values():
            ctx = today_ctx if entity.is_today else tomorrow_ctx
            prev_cons = (
                self.consumption_profiles.today_profiles
                if entity.is_today
                else self.consumption_profiles.tomorrow_profiles
            )
            entity.recalculate(flat_cons, prev_cons, ctx)
