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

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from ..domain import pv_forecast, pv_forecast_extrapolation
from ..domain.charge_slots import ChargeSlots
from ..domain.pv_forecast import PvForecast, WeatherConditionAtHour
from ..domain.weather_forecast_history import WeatherForecastHistory
from ..infrastructure.pv_forecast.consumption_profile_loader import (
    ConsumptionProfileLoader,
)
from ..infrastructure.pv_forecast.live_rate_reader import LiveRateReader
from ..infrastructure.pv_forecast.realized_pv_loader import RealizedPvLoader
from ..infrastructure.pv_forecast.solcast_reader import SolcastReader
from ..infrastructure.weather_listener import WeatherForecastListener

# Backoff between retry attempts when ConsumptionProfiles.refresh_full
# returns a partial result (typical cause: workday calendar / recorder
# not ready at HA startup). MAX_RETRIES is enforced by the entity.
_PROFILE_RETRY_INTERVAL_SEC: float = 60.0

_LOGGER = logging.getLogger(__name__)


class PvForecastService:
    """Orchestrates Solcast/weather reads → PvForecast aggregate updates → listeners."""

    def __init__(
        self,
        hass: HomeAssistant,
        forecast: PvForecast,
        solcast: SolcastReader,
        weather_listener: WeatherForecastListener,
        weather_history: WeatherForecastHistory,
        consumption_loader: ConsumptionProfileLoader,
        live_rates: LiveRateReader,
        realized_pv_loader: RealizedPvLoader,
        charge_slots: ChargeSlots,
    ) -> None:
        self._hass = hass
        self.forecast = forecast
        self._solcast = solcast
        self._weather_listener = weather_listener
        self._weather_history = weather_history
        self._consumption_loader = consumption_loader
        self._live_rates = live_rates
        self._realized_pv_loader = realized_pv_loader
        self._charge_slots = charge_slots
        self._realized_pv_today: dict[tuple[int, int], float] = {}
        # When `refresh_profiles_full` returns a partial result, we
        # schedule a retry via `async_call_later`. The cancel handle is
        # stashed here so a fresh trigger (e.g. daily 05:55 refresh)
        # can supersede a pending retry without firing duplicates.
        self._profile_retry_cancel: Callable[[], None] | None = None
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
            self.forecast.extrapolated_live_pattern = (
                pv_forecast.ExtrapolatedLive.empty()
            )
            self.forecast.extrapolated_live_proportional = (
                pv_forecast.ExtrapolatedLive.empty()
            )
            self.forecast.extrapolated_live_band = pv_forecast.ExtrapolatedLive.empty()
            self.forecast.extrapolated_live_band_recent = (
                pv_forecast.ExtrapolatedLive.empty()
            )
            return

        now = dt_util.now()
        pv_w = self._live_rates.read_pv_power_w()
        cons_w = self._live_rates.read_consumption_w()
        pv_so_far_kwh = self._live_rates.read_pv_bucket_so_far_kwh()
        # Pre-charge gate (used by target_soc calculation inside extrapolations
        # + propagated to non-extrapolated target_soc via `self.forecast`).
        sch = self._live_rates.read_start_charge_hour_today_override()
        self.forecast.start_charge_hour_today = sch
        # Refresh aggregate state used by chart patch + extrapolations
        # (mirrors what `_refresh_start_charge_hour` does for the
        # update_at_6 / update_live paths — the minute tick lands here
        # directly so we set the fields explicitly).
        self.forecast.live_pv_power_w = pv_w
        self.forecast.live_consumption_w = cons_w
        self.forecast.pv_bucket_so_far_kwh = pv_so_far_kwh

        # Domain owns the chart in-progress patch — uniform across `live` +
        # `at_6` + all strategy variants. No-op when live signals missing.
        # update_live / update_at_6 paths already patch on fresh forecast;
        # here the per-minute tick refreshes both with newer pv_w / so_far.
        self.forecast.apply_chart_in_progress_patch(now)

        self.forecast.extrapolated_live_pattern = (
            pv_forecast_extrapolation.extrapolate_calibrated_pattern(
                self.forecast.adjusted_live,
                self.forecast.solcast_live,
                now,
                pv_so_far_kwh,
                self._realized_pv_today,
                pv_power_w_5min=pv_w,
                consumption_w=cons_w,
                start_charge_hour=sch,
            )
        )
        self.forecast.extrapolated_live_proportional = (
            pv_forecast_extrapolation.extrapolate_proportional_median(
                self.forecast.adjusted_live,
                self.forecast.solcast_live,
                now,
                pv_so_far_kwh,
                self._realized_pv_today,
                pv_power_w_5min=pv_w,
                consumption_w=cons_w,
                start_charge_hour=sch,
            )
        )
        self.forecast.extrapolated_live_band = (
            pv_forecast_extrapolation.extrapolate_band_clamped(
                self.forecast.adjusted_live,
                self.forecast.solcast_live,
                now,
                pv_so_far_kwh,
                self._realized_pv_today,
                pv_power_w_5min=pv_w,
                consumption_w=cons_w,
                start_charge_hour=sch,
            )
        )
        self.forecast.extrapolated_live_band_recent = (
            pv_forecast_extrapolation.extrapolate_band_clamped_recent(
                self.forecast.adjusted_live,
                self.forecast.solcast_live,
                now,
                pv_so_far_kwh,
                self._realized_pv_today,
                pv_power_w_5min=pv_w,
                consumption_w=cons_w,
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
        """Refresh forecast.start_charge_hour_{today,tomorrow}.

        Called before each recalc path so target_soc variants inside
        `PvForecast._recalculate_target_soc` see the current pre-charge
        gates:
        - today  : `input_datetime.rce_start_charge_hour_today_override`
          (user manual override, stable HA state outside smart_rce).
        - tomorrow: `ChargeSlots.tomorrow.start_hour` — domain source,
          owned by the same integration. Sourcing from the domain (vs.
          reading `sensor.rce_start_charge_hour_tomorrow_time` we publish
          ourselves) avoids a self-referential race where the sensor is
          still `unavailable` during the first recalc after a reload.
        """
        self.forecast.start_charge_hour_today = (
            self._live_rates.read_start_charge_hour_today_override()
        )
        tomorrow_slot = self._charge_slots.tomorrow
        self.forecast.start_charge_hour_tomorrow = (
            int(tomorrow_slot.start_hour) if tomorrow_slot is not None else None
        )
        # Live signals propagated to the aggregate. Used by:
        # - `ConsumptionProfile.to_view` / `AdjustedPvForecast.to_profile`
        #   in `_recalculate_target_soc` (integrate in-progress vs current power)
        # - `PvForecast.apply_chart_in_progress_patch` (chart in-progress dot
        #   reflects realized so-far + 5-min extrapolation)
        # - extrapolation `_compute_*_score` (current bucket realized rate)
        self.forecast.live_consumption_w = self._live_rates.read_consumption_w()
        self.forecast.live_pv_power_w = self._live_rates.read_pv_power_w()
        self.forecast.pv_bucket_so_far_kwh = (
            self._live_rates.read_pv_bucket_so_far_kwh()
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
    def on_charge_slots_change(self) -> None:
        """Coordinator updated — `ChargeSlots.tomorrow` may have shifted.

        Wired against `rce_coordinator.async_add_listener`. Triggers on
        each successful coordinator refresh; cheap, idempotent. Catches:
        - initial setup race (ChargeSlots is empty during the first
          `recalculate_all` because first_refresh hasn't run yet),
        - runtime shift after the RCE-tomorrow prices arrive ~14:00 and
          the optimal pre-charge window moves.
        """
        self._refresh_start_charge_hour()
        self.forecast._recalculate_target_soc(dt_util.now())  # noqa: SLF001
        self._notify_listeners()

    @callback
    def on_minute_tick(self) -> None:
        """Per-minute cron — refresh extrapolated variants (remaining_fraction shrinks)."""
        self._recalculate_extrapolated()
        self._notify_listeners()

    async def refresh_profiles_full(self) -> None:
        """Full refresh (today + tomorrow anchors) + recalc + listener notify.

        Delegates async I/O to the `ConsumptionProfiles` entity (rich
        domain model). On a partial outcome (any prev_X slot left None
        — typical when called before workday calendar / recorder fully
        loaded at HA startup), schedules a retry 60s later. Caps total
        attempts via `ConsumptionProfiles.MAX_RETRIES`.
        """
        now = dt_util.now()
        try:
            await self.forecast.consumption_profiles.refresh_full(
                self._consumption_loader, now
            )
        except Exception:  # noqa: BLE001 — defensive, don't crash integration
            _LOGGER.exception("Failed to refresh consumption profiles (full)")
            return
        self.forecast.recalculate_target_soc(now)
        self._notify_listeners()
        self._maybe_schedule_profile_retry()

    async def refresh_profiles_tomorrow_only(self) -> None:
        """Tomorrow-anchored only refresh — for bucket-boundary intraday updates.

        Called at each :00 / :30 inside the 07:30..13:30 PV window
        (today's profile, used as tomorrow_prev_1, grows as utility
        meter cycles close). Today-anchored profiles never change
        during the day so we skip the second fetch — single recorder
        round-trip vs `refresh_profiles_full`'s two.
        """
        now = dt_util.now()
        try:
            await self.forecast.consumption_profiles.refresh_tomorrow_only(
                self._consumption_loader, now
            )
        except Exception:  # noqa: BLE001 — defensive
            _LOGGER.exception("Failed to refresh consumption profiles (tomorrow)")
            return
        self.forecast.recalculate_target_soc(now)
        self._notify_listeners()

    def _maybe_schedule_profile_retry(self) -> None:
        """Schedule another `refresh_profiles_full` if the entity says so.

        The entity's `should_retry()` checks both partial-state and the
        retry budget. Existing pending retry is cancelled so a fresh
        scheduled trigger (daily 05:55) doesn't stack with it.
        """
        if not self.forecast.consumption_profiles.should_retry():
            self._cancel_profile_retry()
            return
        self._cancel_profile_retry()
        attempt = self.forecast.consumption_profiles.failed_attempts
        _LOGGER.warning(
            "Consumption profile refresh partial (attempt %d/%d) — "
            "retrying in %.0fs",
            attempt,
            self.forecast.consumption_profiles.MAX_RETRIES,
            _PROFILE_RETRY_INTERVAL_SEC,
        )

        @callback
        def _on_retry(_now) -> None:
            self._profile_retry_cancel = None
            self._hass.async_create_task(self.refresh_profiles_full())

        self._profile_retry_cancel = async_call_later(
            self._hass, _PROFILE_RETRY_INTERVAL_SEC, _on_retry
        )

    def _cancel_profile_retry(self) -> None:
        if self._profile_retry_cancel is not None:
            self._profile_retry_cancel()
            self._profile_retry_cancel = None

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        """Register listener for forecast/SoC change events. Returns unsubscribe fn."""

        def remove_listener() -> None:
            self._listeners.pop(remove_listener)

        self._listeners[remove_listener] = update_callback
        return remove_listener

    def _notify_listeners(self) -> None:
        for update_callback in list(self._listeners.values()):
            update_callback()
