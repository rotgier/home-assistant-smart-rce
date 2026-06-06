"""PvForecastUpdater — aggregate owning all PV forecast scenarios.

DDD split from `TargetSocCatalog`: catalog owns the "what PV looks like"
concern (8 forecast strategies + extrapolation + PV-side live signals),
while `TargetSocCatalog` shrinks to the "what battery target SoC results from
forecast + consumption" concern.

Read API is strategy-enum-keyed so consumers (TargetSoc derivation,
future ChargePlanner, dashboard matrix) don't need to know about the
internal 8 fields — they ask `catalog.get(PvForecast.LIVE)` /
`catalog.today()` / `catalog.tomorrow()`.

Update API is trigger-source-named, not strategy-named: service
callbacks (Solcast at_6 change, Solcast live change, weather refresh,
per-minute tick) match HA events, and catalog owns the
trigger→strategy mapping internally. Adding a new EXTRAP variant is a
catalog-internal change; service callbacks never need to know.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from . import pv_forecast_extrapolation
from .pv_forecast import (
    AT6_CLOUDY_MODIFIER_EARLY,
    AT6_CLOUDY_MODIFIER_LATE,
    CLOUDY_CAP_HOUR_7,
    EXTRAP_STRATEGIES,
    PARTLY_CONDITIONS,
    PARTLY_VARIABLE_CONDITIONS,
    SUNNY_CONDITIONS,
    TODAY_STRATEGIES,
    TOMORROW_STRATEGIES,
    AdjustedPeriod,
    AdjustedPvForecast,
    ExtrapolatedLive,
    LivePvSignals,
    PvForecast,
    SolcastPeriod,
    WeatherConditionAtHour,
)

__all__ = [
    "EXTRAP_STRATEGIES",
    "LivePvSignals",
    "PvForecastUpdater",
    "PvForecast",
    "TODAY_STRATEGIES",
    "TOMORROW_STRATEGIES",
]


def _empty_forecasts() -> dict[PvForecast, AdjustedPvForecast | None]:
    """All non-extrap strategies → None (initial state before any update)."""
    return {
        strategy: None for strategy in PvForecast if strategy not in EXTRAP_STRATEGIES
    }


def _empty_extrapolated() -> dict[PvForecast, ExtrapolatedLive]:
    """All extrap strategies → ExtrapolatedLive.empty (matches TargetSocCatalog default)."""
    return {strategy: ExtrapolatedLive.empty() for strategy in EXTRAP_STRATEGIES}


@dataclass
class PvForecastUpdater:
    """Aggregate owning all PV forecast scenarios + their compute pipeline."""

    # — Private state (single underscore = Python "private" convention) —
    _signals: LivePvSignals = field(default_factory=LivePvSignals)
    _forecasts: dict[PvForecast, AdjustedPvForecast | None] = field(
        default_factory=_empty_forecasts
    )
    _extrapolated: dict[PvForecast, ExtrapolatedLive] = field(
        default_factory=_empty_extrapolated
    )
    # Raw Solcast live periods — needed by extrapolation (uses pv_estimate +
    # pv_estimate10 raw quantiles, not the weather-adjusted output).
    _solcast_live: list[SolcastPeriod] = field(default_factory=list)

    # ─── Read API ──────────────────────────────────────────────────────────

    def get(self, strategy: PvForecast) -> AdjustedPvForecast | None:
        """Return adjusted forecast for `strategy`, or None if not yet computed.

        For EXTRAP_* strategies returns the bundled `.adjusted` field
        (chart-facing variant) — same shape as AT_6 / LIVE.
        """
        if strategy in EXTRAP_STRATEGIES:
            return self._extrapolated[strategy].adjusted
        return self._forecasts.get(strategy)

    def get_extrapolated(self, strategy: PvForecast) -> ExtrapolatedLive | None:
        """Return full ExtrapolatedLive bundle for an EXTRAP_* strategy.

        Bundles `adjusted` + `remaining_kwh` + `target_soc`. Used by sensors
        that need state/SOC alongside the chart-facing forecast.
        """
        if strategy not in EXTRAP_STRATEGIES:
            return None
        return self._extrapolated.get(strategy)

    def all(self) -> dict[PvForecast, AdjustedPvForecast | None]:
        """Snapshot dict of every strategy → forecast (or None)."""
        result: dict[PvForecast, AdjustedPvForecast | None] = dict(self._forecasts)
        for strategy in EXTRAP_STRATEGIES:
            result[strategy] = self._extrapolated[strategy].adjusted
        return result

    def today(self) -> dict[PvForecast, AdjustedPvForecast | None]:
        """Snapshot of today-axis strategies (AT_6, LIVE, 4× EXTRAP)."""
        return {s: self.get(s) for s in TODAY_STRATEGIES}

    def tomorrow(self) -> dict[PvForecast, AdjustedPvForecast | None]:
        """Snapshot of tomorrow-axis strategies (TOMORROW_AT_6, TOMORROW_LIVE)."""
        return {s: self.get(s) for s in TOMORROW_STRATEGIES}

    @property
    def signals(self) -> LivePvSignals:
        """Read-only snapshot of current PV-side live readings."""
        return self._signals

    @property
    def solcast_live(self) -> list[SolcastPeriod]:
        """Raw Solcast live periods — exposed for downstream consumers."""
        return self._solcast_live

    # ─── Update methods — named by TRIGGER SOURCE ──────────────────────────

    def refresh_live_signals(self, signals: LivePvSignals) -> None:
        """Atomic snapshot of 4 PV-side live readings (single VO write)."""
        self._signals = signals

    def update_from_solcast_at_6(
        self,
        solcast_periods: list[SolcastPeriod],
        weather_conditions: list[WeatherConditionAtHour],
        now: datetime,
    ) -> None:
        """Triggered when Solcast at_6 entity changes (~once daily 06:00).

        Internally touches AT_6 (today-axis morning prediction). Tomorrow
        AT_6 is fed via `update_from_solcast_tomorrow` because the at_6 entity
        carries today-only periods (Solcast publishes a separate "tomorrow"
        entity for that side — see service callback wiring).
        """
        adjusted = self._adjust_pv_forecast_at6(solcast_periods, weather_conditions)
        self._forecasts[PvForecast.AT_6] = self._apply_chart_in_progress_patch(
            now, adjusted
        )

    def update_from_solcast_live(
        self,
        solcast_periods: list[SolcastPeriod],
        weather_conditions: list[WeatherConditionAtHour],
        now: datetime,
    ) -> None:
        """Triggered when Solcast live entity changes (continuous updates).

        Internally touches LIVE + raw _solcast_live (preserved for extrap input).
        """
        self._solcast_live = solcast_periods
        adjusted = self._adjust_pv_forecast_live(
            solcast_periods, weather_conditions, now
        )
        self._forecasts[PvForecast.LIVE] = self._apply_chart_in_progress_patch(
            now, adjusted
        )

    def update_from_solcast_tomorrow(
        self,
        solcast_periods: list[SolcastPeriod],
        weather_conditions: list[WeatherConditionAtHour],
        now: datetime,
    ) -> None:
        """Triggered when Solcast tomorrow entity changes.

        Touches TOMORROW_AT_6 (AT6 modifiers, pessimistic) + TOMORROW_LIVE
        (LIVE modifiers, optimistic). Two variants from one source: AT6
        modifiers serve evening planning safety lower-bound; LIVE modifiers
        align with target_soc_live after midnight rollover.
        """
        self._forecasts[PvForecast.TOMORROW_AT_6] = self._adjust_pv_forecast_at6(
            solcast_periods, weather_conditions
        )
        # _adjust_pv_forecast_live checks is_first_hour = (period.hour == now.hour).
        # For tomorrow's periods (date = tomorrow) no match → all periods use
        # standard LIVE modifiers (no special first-hour treatment).
        self._forecasts[PvForecast.TOMORROW_LIVE] = self._adjust_pv_forecast_live(
            solcast_periods, weather_conditions, now
        )

    def apply_chart_in_progress_patch(self, now: datetime) -> None:
        """Refresh in-progress period of every today adjusted variant in place.

        Service per-minute hook — single call rescales LIVE AND AT_6 to reflect
        newer pv_power_w / bucket_so_far_kwh. No-op for variants currently set
        to None (early startup before the first solcast update).
        """
        live = self._forecasts.get(PvForecast.LIVE)
        if live is not None:
            self._forecasts[PvForecast.LIVE] = self._apply_chart_in_progress_patch(
                now, live
            )
        at_6 = self._forecasts.get(PvForecast.AT_6)
        if at_6 is not None:
            self._forecasts[PvForecast.AT_6] = self._apply_chart_in_progress_patch(
                now, at_6
            )

    def tick_minute(
        self,
        now: datetime,
        realized_pv_today: dict[tuple[int, int], float],
        consumption_w: float | None,
        start_charge_hour: int | None,
    ) -> None:
        """Per-minute orchestration: recompute 4 EXTRAP variants + chart patch.

        Uses INTERNAL adjusted LIVE + raw solcast_live + signals — service does
        NOT pull fields to feed pure functions externally. Cross-cutting args
        (realized_pv_today, consumption_w, start_charge_hour) live outside
        this aggregate's bounded context and arrive as call-args.

        No-op when LIVE forecast not yet computed (early startup race).
        """
        adjusted_live = self._forecasts.get(PvForecast.LIVE)
        if adjusted_live is None:
            return
        pv_w = self._signals.pv_power_w
        so_far = self._signals.bucket_so_far_kwh
        self._extrapolated[PvForecast.EXTRAP_PATTERN] = (
            pv_forecast_extrapolation.extrapolate_calibrated_pattern(
                adjusted_live,
                self._solcast_live,
                now,
                so_far,
                realized_pv_today,
                pv_power_w_5min=pv_w,
                consumption_w=consumption_w,
                start_charge_hour=start_charge_hour,
            )
        )
        self._extrapolated[PvForecast.EXTRAP_PROPORTIONAL] = (
            pv_forecast_extrapolation.extrapolate_proportional_median(
                adjusted_live,
                self._solcast_live,
                now,
                so_far,
                realized_pv_today,
                pv_power_w_5min=pv_w,
                consumption_w=consumption_w,
                start_charge_hour=start_charge_hour,
            )
        )
        self._extrapolated[PvForecast.EXTRAP_BAND] = (
            pv_forecast_extrapolation.extrapolate_band_clamped(
                adjusted_live,
                self._solcast_live,
                now,
                so_far,
                realized_pv_today,
                pv_power_w_5min=pv_w,
                consumption_w=consumption_w,
                start_charge_hour=start_charge_hour,
            )
        )
        self._extrapolated[PvForecast.EXTRAP_BAND_RECENT] = (
            pv_forecast_extrapolation.extrapolate_band_clamped_recent(
                adjusted_live,
                self._solcast_live,
                now,
                so_far,
                realized_pv_today,
                pv_power_w_5min=pv_w,
                consumption_w=consumption_w,
                start_charge_hour=start_charge_hour,
            )
        )
        # Chart in-progress patch for today's non-extrap variants (LIVE, AT_6)
        # follows extrap recompute — same per-tick cadence.
        self.apply_chart_in_progress_patch(now)

    # ─── Internal helpers (moved from TargetSocCatalog) ──────────────────────────

    def _apply_chart_in_progress_patch(
        self, now: datetime, adjusted: AdjustedPvForecast
    ) -> AdjustedPvForecast:
        """Return `adjusted` with in-progress period rescaled, or unchanged.

        Rescale = full-bucket estimate (realized so-far + remaining via
        5-min power); unchanged when live signals aren't set.
        """
        pv_w = self._signals.pv_power_w
        so_far = self._signals.bucket_so_far_kwh
        if pv_w is None or so_far is None:
            return adjusted
        return adjusted.with_now_aware_in_progress(
            now=now,
            pv_power_w_5min=pv_w,
            pv_bucket_so_far_kwh=so_far,
        )

    @staticmethod
    def _adjust_pv_forecast_at6(
        solcast_periods: list[SolcastPeriod],
        weather_conditions: list[WeatherConditionAtHour],
    ) -> AdjustedPvForecast:
        """Adjust morning Solcast forecast (snapshot from 6:05) using weather."""
        forecast = []
        total_kwh = 0.0

        for period in solcast_periods:
            dt = datetime.fromisoformat(period.period_start)
            hour = dt.hour
            target_date = dt.date()
            condition = _get_condition_for_hour(hour, weather_conditions, target_date)
            adj_rate = _adjust_at6_period(period, condition, hour)

            forecast.append(
                AdjustedPeriod(
                    period_start=period.period_start,
                    pv_estimate_adjusted=round(adj_rate, 4),
                )
            )
            total_kwh += adj_rate / 2  # rate -> kWh per 30min

        return AdjustedPvForecast(forecast=forecast, total_kwh=round(total_kwh, 4))

    @staticmethod
    def _adjust_pv_forecast_live(
        solcast_periods: list[SolcastPeriod],
        weather_conditions: list[WeatherConditionAtHour],
        now: datetime,
    ) -> AdjustedPvForecast:
        """Adjust live Solcast forecast using weather. First hour treated differently."""
        forecast = []
        total_kwh = 0.0
        current_hour = now.hour

        for period in solcast_periods:
            dt = datetime.fromisoformat(period.period_start)
            hour = dt.hour
            target_date = dt.date()
            is_first_hour = hour == current_hour
            condition = _get_condition_for_hour(hour, weather_conditions, target_date)
            adj_rate = _adjust_live_period(period, condition, is_first_hour)

            forecast.append(
                AdjustedPeriod(
                    period_start=period.period_start,
                    pv_estimate_adjusted=round(adj_rate, 4),
                )
            )
            total_kwh += adj_rate / 2

        return AdjustedPvForecast(forecast=forecast, total_kwh=round(total_kwh, 4))


# --- Module-level helpers (multi-class users, no state) --- #


def _classify_condition(condition: str) -> str:
    """Classify condition into: sunny, partly-variable, partly, cloudy."""
    if condition in SUNNY_CONDITIONS:
        return "sunny"
    if condition in PARTLY_VARIABLE_CONDITIONS:
        return "partly-variable"
    if condition in PARTLY_CONDITIONS:
        return "partly"
    return "cloudy"


def _get_condition_for_hour(
    hour: int,
    weather_conditions: list[WeatherConditionAtHour],
    target_date: date | None = None,
) -> str:
    """Find weather condition for given hour and date. Fallback to cloudy."""
    # Exact match: date + hour
    if target_date:
        for w in weather_conditions:
            if w.forecast_date == target_date and w.hour == hour:
                return w.condition_custom
    # Fallback: hour only (for conditions without date)
    for w in weather_conditions:
        if w.hour == hour and w.forecast_date is None:
            return w.condition_custom
    return "cloudy"


def _adjust_at6_period(period: SolcastPeriod, condition: str, hour: int) -> float:
    """Apply AT6 weather adjustment. Returns adjusted hourly rate."""
    cat = _classify_condition(condition)

    if cat == "sunny":
        return period.pv_estimate * 1.0
    if cat == "partly-variable":
        return period.pv_estimate * 0.8
    if cat == "partly":
        return period.pv_estimate * 0.7

    # cloudy/other
    if hour <= 10:
        modifier = AT6_CLOUDY_MODIFIER_EARLY
    else:
        modifier = AT6_CLOUDY_MODIFIER_LATE

    adj = period.pv_estimate10 * modifier

    if hour == 7:
        adj = min(adj, CLOUDY_CAP_HOUR_7)

    return adj


def _adjust_live_period(
    period: SolcastPeriod, condition: str, is_first_hour: bool
) -> float:
    """Apply LIVE weather adjustment. Returns adjusted hourly rate."""
    cat = _classify_condition(condition)

    if is_first_hour:
        # Trust Solcast for the next hour, only swap est->est10 for cloudy
        if cat == "cloudy":
            return period.pv_estimate10 * 1.0
        return period.pv_estimate * 1.0

    # Remaining hours
    if cat == "sunny":
        return period.pv_estimate * 1.0
    if cat == "partly-variable":
        return period.pv_estimate * 0.8
    if cat == "partly":
        return period.pv_estimate * 0.7

    # cloudy/other — est10 without additional modifier
    return period.pv_estimate10 * 1.0
