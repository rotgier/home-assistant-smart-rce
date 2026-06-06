"""TargetSocCatalog — aggregate owning target_soc derivation.

DDD split from `PvForecastUpdater`: catalog owns the "what battery target
SoC results from forecast + consumption" concern (target_soc_* cache +
consumption profiles + cons-side live signal + pre-charge gates), while
`PvForecastUpdater` owns the "what PV looks like" concern (forecast
scenarios + extrapolation + PV-side live signals).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .consumption_profiles import (
    PREV_DAYS_COUNT,
    ConsumptionProfile,
    ConsumptionProfiles,
)
from .pv_forecast import TargetSocInputs
from .pv_forecast_strategy import PvForecast
from .target_soc import TargetSocResult, calculate_target_soc

if TYPE_CHECKING:
    from .pv_forecast_catalog import PvForecastUpdater


@dataclass
class TargetSocCatalog:
    """Aggregate owning TargetSoc derivation from forecast updater + consumption baselines.

    Reads PV forecast scenarios + PV-side live signals from
    `PvForecastUpdater` (collaborator). Owns the consumption side:
    cons-side live signal + start_charge_hour gates (via `TargetSocInputs`),
    consumption baselines (rich `ConsumptionProfiles` entity), and the
    derived `target_soc_*` cache.

    `target_soc_*` field naming: `at_6` / `live` suffix on every variant
    (no implicit "default") — symmetric across today and tomorrow axes.
    """

    _inputs: TargetSocInputs = field(default_factory=TargetSocInputs)
    consumption_profiles: ConsumptionProfiles = field(
        default_factory=lambda: ConsumptionProfiles.empty()
    )
    target_soc_at_6: TargetSocResult | None = None
    target_soc_live: TargetSocResult | None = None
    target_soc_tomorrow_at_6: TargetSocResult | None = None
    target_soc_tomorrow_live: TargetSocResult | None = None
    target_soc_prev_days: list[TargetSocResult | None] = field(
        default_factory=lambda: [None] * PREV_DAYS_COUNT
    )
    target_soc_tomorrow_prev_days: list[TargetSocResult | None] = field(
        default_factory=lambda: [None] * PREV_DAYS_COUNT
    )
    target_soc_max: int | None = None
    target_soc_tomorrow_max: int | None = None

    # — Read accessor —

    @property
    def inputs(self) -> TargetSocInputs:
        """Read-only snapshot of cons-side live + pre-charge gates."""
        return self._inputs

    # — Update methods (Tell-Don't-Ask) —

    def refresh_inputs(self, inputs: TargetSocInputs) -> None:
        """Atomic snapshot of cons-side live + pre-charge gates."""
        self._inputs = inputs

    def recalculate_target_soc(self, updater: PvForecastUpdater, now: datetime) -> None:
        """Recompute target_soc_* cache from updater forecasts + consumption profiles.

        Public hook used by `ConsumptionProfiles.refresh_*` callers and by
        application service after every updater update. The entity mutates
        `consumption_profiles.today_profiles` / `tomorrow_profiles` in
        place; the aggregate then refreshes its downstream `target_soc_*`
        cache via this method.

        Today variants build now-aware profiles via
        `AdjustedPvForecast.to_profile(today, now, pv_power_w_5min=...)`
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
        sch = self._inputs.start_charge_hour_today
        sch_t = self._inputs.start_charge_hour_tomorrow
        live_cons_w = self._inputs.live_consumption_w
        live_pv_w = updater.signals.pv_power_w
        default_cons = ConsumptionProfile.flat()
        today = now.date()
        tomorrow = today + timedelta(days=1)

        adjusted_at_6 = updater.get(PvForecast.AT_6)
        adjusted_live = updater.get(PvForecast.LIVE)
        adjusted_tomorrow_at_6 = updater.get(PvForecast.TOMORROW_AT_6)
        adjusted_tomorrow_live = updater.get(PvForecast.TOMORROW_LIVE)

        # Today block — needs both live signals or sets to None
        today_ready = live_cons_w is not None and live_pv_w is not None
        if today_ready:
            cons_view_today = default_cons.to_view(
                now=now, live_consumption_w=live_cons_w
            )
            at6_profile = (
                adjusted_at_6.to_profile(today, now=now, pv_power_w_5min=live_pv_w)
                if adjusted_at_6
                else None
            )
            live_profile = (
                adjusted_live.to_profile(today, now=now, pv_power_w_5min=live_pv_w)
                if adjusted_live
                else None
            )
            self.target_soc_at_6 = (
                calculate_target_soc(
                    at6_profile, cons_view_today, start_charge_hour=sch
                )
                if at6_profile is not None
                else None
            )
            self.target_soc_live = (
                calculate_target_soc(
                    live_profile, cons_view_today, start_charge_hour=sch
                )
                if live_profile is not None
                else None
            )
        else:
            live_profile = None
            self.target_soc_at_6 = None
            self.target_soc_live = None

        # Tomorrow: full 7-13 window; no live override (current power doesn't
        # carry across days). Plain profile snapshots — `now=None` path.
        tomorrow_live_profile = (
            adjusted_tomorrow_live.to_profile(tomorrow)
            if adjusted_tomorrow_live
            else None
        )
        if adjusted_tomorrow_at_6:
            self.target_soc_tomorrow_at_6 = calculate_target_soc(
                adjusted_tomorrow_at_6.to_profile(tomorrow),
                default_cons,
                start_charge_hour=sch_t,
            )
        else:
            self.target_soc_tomorrow_at_6 = None
        if tomorrow_live_profile is not None:
            self.target_soc_tomorrow_live = calculate_target_soc(
                tomorrow_live_profile, default_cons, start_charge_hour=sch_t
            )
        else:
            self.target_soc_tomorrow_live = None

        # Prev-workday instrumentation. Two anchor sets:
        # - today_profiles: anchored at today → prev_1 = yesterday workday
        # - tomorrow_profiles: anchored at tomorrow → prev_1 = today workday
        for i, profile in enumerate(self.consumption_profiles.today_profiles):
            if profile is None or live_profile is None or not today_ready:
                self.target_soc_prev_days[i] = None
                continue
            assert live_cons_w is not None  # narrowed by today_ready
            self.target_soc_prev_days[i] = calculate_target_soc(
                live_profile,
                profile.to_view(now=now, live_consumption_w=live_cons_w),
                start_charge_hour=sch,
            )
        for i, profile in enumerate(self.consumption_profiles.tomorrow_profiles):
            if tomorrow_live_profile is not None and profile is not None:
                self.target_soc_tomorrow_prev_days[i] = calculate_target_soc(
                    tomorrow_live_profile,
                    profile,
                    start_charge_hour=sch_t,
                )
            else:
                self.target_soc_tomorrow_prev_days[i] = None

        today_vals = [
            r.value
            for r in [self.target_soc_live, *self.target_soc_prev_days]
            if r is not None
        ]
        self.target_soc_max = max(today_vals) if today_vals else None
        tmrw_vals = [
            r.value
            for r in [
                self.target_soc_tomorrow_live,
                *self.target_soc_tomorrow_prev_days,
            ]
            if r is not None
        ]
        self.target_soc_tomorrow_max = max(tmrw_vals) if tmrw_vals else None
