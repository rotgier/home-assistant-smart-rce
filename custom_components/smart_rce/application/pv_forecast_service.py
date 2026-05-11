"""PvForecastService — application service orchestrating weather-adjusted PV estimates.

DDD application layer (analog Ems in application/ems.py): read from driving
adapters (Solcast, weather) → call domain `PvForecast.update_*` → notify
listeners. State + algorithms live in domain — Service is pure orchestration.

HASS-FREE: dependencies injected by `pv_forecast_factory.py` (composition
root). Service does not know about HomeAssistant — sync→async wrapping
(`hass.async_create_task` for daily refresh) is done in the factory.

Public callbacks (`on_*`) are wired in the factory:
- weather_listener.async_add_listener(service.on_weather_update)
- async_track_state_change_event(SOLCAST_*, service.on_solcast_*_change)
- async_track_time_change(05:55, factory wrapper → service.refresh_profiles)
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
import logging

from homeassistant.core import CALLBACK_TYPE, Event, callback
from homeassistant.util import dt as dt_util

from ..domain import pv_forecast, pv_forecast_extrapolation
from ..domain.pv_forecast import PvForecast, WeatherConditionAtHour
from ..domain.weather_forecast_history import WeatherForecastHistory
from ..infrastructure.pv_forecast.consumption_profile_loader import (
    ConsumptionProfileLoader,
)
from ..infrastructure.pv_forecast.live_rate_reader import LiveRateReader
from ..infrastructure.pv_forecast.realized_pv_loader import RealizedPvLoader
from ..infrastructure.pv_forecast.solcast_reader import SolcastReader
from ..infrastructure.weather_listener import WeatherForecastListener

_LOGGER = logging.getLogger(__name__)


class PvForecastService:
    """Orchestrates Solcast/weather reads → PvForecast aggregate updates → listeners."""

    def __init__(
        self,
        forecast: PvForecast,
        solcast: SolcastReader,
        weather_listener: WeatherForecastListener,
        weather_history: WeatherForecastHistory,
        consumption_loader: ConsumptionProfileLoader,
        live_rates: LiveRateReader,
        realized_pv_loader: RealizedPvLoader,
    ) -> None:
        self.forecast = forecast
        self._solcast = solcast
        self._weather_listener = weather_listener
        self._weather_history = weather_history
        self._consumption_loader = consumption_loader
        self._live_rates = live_rates
        self._realized_pv_loader = realized_pv_loader
        self._realized_pv_today: dict[tuple[int, int], float] = {}
        self._listeners: dict[CALLBACK_TYPE, CALLBACK_TYPE] = {}

    def recalculate_all(self) -> None:
        """Recalculate all forecasts (at_6, live, tomorrow) + extrapolated + notify."""
        self._recalculate_at6()
        self._recalculate_live()
        self._recalculate_tomorrow()
        self._recalculate_extrapolated()
        self._notify_listeners()

    def _recalculate_extrapolated(self) -> None:
        """Recompute extrapolated live variants — called every minute + after forecast updates.

        Synchronous — operates on cached `_realized_pv_today` (refreshed by
        `refresh_realized_pv` async path on minute tick / startup).
        """
        if not self.forecast.adjusted_live:
            self.forecast.extrapolated_live = pv_forecast.ExtrapolatedLive.empty()
            self.forecast.extrapolated_live_5min = pv_forecast.ExtrapolatedLive.empty()
            self.forecast.extrapolated_live_pattern = (
                pv_forecast.ExtrapolatedLive.empty()
            )
            return

        now = dt_util.now()
        pv_w = self._live_rates.read_pv_power_w()
        cons_w = self._live_rates.read_consumption_w()
        pv_so_far_kwh = self._live_rates.read_pv_bucket_so_far_kwh()
        cons_so_far_kwh = self._live_rates.read_consumption_bucket_so_far_kwh()
        # Pre-charge gate (used by target_soc calculation inside extrapolations
        # + propagated to non-extrapolated target_soc via `self.forecast`).
        sch = self._live_rates.read_start_charge_hour_today_override()
        self.forecast.start_charge_hour_today = sch

        self.forecast.extrapolated_live = (
            pv_forecast_extrapolation.extrapolate_realized_prorate(
                self.forecast.adjusted_live,
                now,
                pv_so_far_kwh,
                cons_so_far_kwh,
                start_charge_hour=sch,
            )
        )
        self.forecast.extrapolated_live_5min = (
            pv_forecast_extrapolation.extrapolate_5min_rate(
                self.forecast.adjusted_live,
                now,
                pv_w,
                cons_w,
                start_charge_hour=sch,
            )
        )
        self.forecast.extrapolated_live_pattern = (
            pv_forecast_extrapolation.extrapolate_calibrated_pattern(
                self.forecast.adjusted_live,
                self.forecast.solcast_live,
                now,
                pv_so_far_kwh,
                cons_so_far_kwh,
                self._realized_pv_today,
                start_charge_hour=sch,
            )
        )
        self.forecast.extrapolated_live_proportional = (
            pv_forecast_extrapolation.extrapolate_proportional_median(
                self.forecast.adjusted_live,
                self.forecast.solcast_live,
                now,
                pv_so_far_kwh,
                cons_so_far_kwh,
                self._realized_pv_today,
                start_charge_hour=sch,
            )
        )
        self.forecast.extrapolated_live_band = (
            pv_forecast_extrapolation.extrapolate_band_clamped(
                self.forecast.adjusted_live,
                self.forecast.solcast_live,
                now,
                pv_so_far_kwh,
                cons_so_far_kwh,
                self._realized_pv_today,
                start_charge_hour=sch,
            )
        )
        self.forecast.extrapolated_live_band_recent = (
            pv_forecast_extrapolation.extrapolate_band_clamped_recent(
                self.forecast.adjusted_live,
                self.forecast.solcast_live,
                now,
                pv_so_far_kwh,
                cons_so_far_kwh,
                self._realized_pv_today,
                start_charge_hour=sch,
            )
        )

    async def refresh_realized_pv(self) -> None:
        """Fetch today's realized PV per bucket from recorder; cache for next recalc."""
        try:
            self._realized_pv_today = await self._realized_pv_loader.fetch_today(
                dt_util.now().date()
            )
        except Exception:  # noqa: BLE001 — defensive, don't crash integration
            _LOGGER.exception("Failed to fetch realized PV history")

    def _refresh_start_charge_hour(self) -> None:
        """Refresh forecast.start_charge_hour_today from HA state.

        Called before each recalc path so non-extrapolated target_soc variants
        (target_soc, target_soc_live, target_soc_prev_days) inside
        `PvForecast._recalculate_target_soc` see the current pre-charge gate.
        """
        self.forecast.start_charge_hour_today = (
            self._live_rates.read_start_charge_hour_today_override()
        )

    def _recalculate_at6(self) -> None:
        """Recalculate AT6 forecast.

        Before 6:01 — use live Solcast (has forecast fetched at 22:00).
        After 6:01 — use at_6 snapshot (fresh for today).
        """
        now = dt_util.now()
        if now.hour < 6 or (now.hour == 6 and now.minute < 2):
            solcast_periods = self._solcast.read_live()
        else:
            solcast_periods = self._solcast.read_at_6()
        if not solcast_periods:
            return
        weather = self._build_weather(now.date())
        self._refresh_start_charge_hour()
        self.forecast.update_at_6(solcast_periods, weather, now)

    def _recalculate_live(self) -> None:
        """Recalculate live weather-adjusted forecast."""
        solcast_periods = self._solcast.read_live()
        if not solcast_periods:
            return
        now = dt_util.now()
        weather = self._build_weather(now.date())
        self._refresh_start_charge_hour()
        self.forecast.update_live(solcast_periods, weather, now)

    def _recalculate_tomorrow(self) -> None:
        """Recalculate tomorrow forecast — both AT6 and LIVE variants."""
        solcast_periods = self._solcast.read_tomorrow()
        if not solcast_periods:
            return
        now = dt_util.now()
        tomorrow = (now + timedelta(days=1)).date()
        weather = self._build_weather(tomorrow)
        self._refresh_start_charge_hour()
        self.forecast.update_tomorrow(solcast_periods, weather, now)

    def _build_weather(self, day: date) -> list[WeatherConditionAtHour]:
        """Combine weather history (past hours) + live forecast (future hours).

        Multi-caller helper — used by 3× `_recalculate_*`. Pure orchestration:
        read 2 sources + delegate merge to domain `merge_weather_conditions`.
        """
        history = self._weather_history.get_conditions_for_date(day)
        forecast = self._weather_listener.forecast_conditions
        return pv_forecast.merge_weather_conditions(history, forecast)

    @callback
    def on_weather_update(self) -> None:
        """Weather forecast changed — recalculate all (history+forecast affects all)."""
        self.recalculate_all()

    @callback
    def on_solcast_at6_change(self, event: Event) -> None:
        """Solcast at_6 snapshot changed — recalculate at_6."""
        self._recalculate_at6()
        self._notify_listeners()

    @callback
    def on_solcast_live_change(self, event: Event) -> None:
        """Solcast live changed — recalculate live + extrapolated."""
        self._recalculate_live()
        self._recalculate_extrapolated()
        self._notify_listeners()

    @callback
    def on_solcast_tomorrow_change(self, event: Event) -> None:
        """Solcast tomorrow changed — recalculate tomorrow."""
        self._recalculate_tomorrow()
        self._notify_listeners()

    @callback
    def on_minute_tick(self) -> None:
        """Per-minute cron — refresh extrapolated variants (remaining_fraction shrinks)."""
        self._recalculate_extrapolated()
        self._notify_listeners()

    async def refresh_profiles(self) -> None:
        """Fetch prev-workday consumption profiles + update aggregate + notify."""
        now = dt_util.now()
        try:
            profiles = await self._consumption_loader.fetch(now.date())
        except Exception:  # noqa: BLE001 — defensive, don't crash integration
            _LOGGER.exception("Failed to fetch consumption profiles")
            return
        self.forecast.update_consumption_profiles(profiles, now)
        self._notify_listeners()

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        """Register listener for forecast/SoC change events. Returns unsubscribe fn."""

        def remove_listener() -> None:
            self._listeners.pop(remove_listener)

        self._listeners[remove_listener] = update_callback
        return remove_listener

    def _notify_listeners(self) -> None:
        for update_callback in list(self._listeners.values()):
            update_callback()
