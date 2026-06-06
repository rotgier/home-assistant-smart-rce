"""PvForecastService — application service orchestrating PV forecast pipeline.

DDD application layer. Reads driving adapters (Solcast, weather, live rates,
charge slots) and pushes data as semantic VOs to two domain aggregates:

- `PvForecastUpdater` — owns "what PV looks like": 8 forecast strategies +
  extrapolation + PV-side live signals (live_pv_power_w, bucket_so_far,
  derivative, stability).
- `TargetSocCatalog` — owns "what battery target SoC results from forecast +
  consumption": target_soc_* cache + consumption profiles + cons-side live
  signal + pre-charge gates.

Service writes via VOs (`LivePvSignals`, `TargetSocInputs`), never field-by-
field. Catalog update methods are trigger-source-named (match HA events);
service does NOT know which strategies a trigger touches.

Update sequence per tick:
1. `_refresh_inputs()` — read 4 PV signals + 1 cons signal + 2 start_charge
   gates from `LiveRateReader` / `ChargeSlots`; push to catalog/forecast as VOs.
2. `catalog.update_from_X(...)` or `catalog.tick_minute(...)` — catalog
   refreshes affected forecast strategies + raw solcast_live.
3. `forecast.recalculate_target_soc(catalog, now)` — derive target_soc_*
   from catalog state + consumption profiles.
4. `_notify_listeners()` — fan out to sensors.

HASS-FREE: dependencies injected by `pv_forecast_factory.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
import logging

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from ..domain import pv_forecast
from ..domain.charge_slots import ChargeSlots
from ..domain.pv_forecast import LivePvSignals, TargetSocInputs, WeatherConditionAtHour
from ..domain.pv_forecast_catalog import PvForecastUpdater
from ..domain.target_soc_catalog import TargetSocCatalog
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
    """Orchestrates Solcast/weather reads → catalog updates → target_soc recalc → listeners."""

    def __init__(
        self,
        hass: HomeAssistant,
        updater: PvForecastUpdater,
        target_socs: TargetSocCatalog,
        solcast: SolcastReader,
        weather_listener: WeatherForecastListener,
        weather_history: WeatherForecastHistory,
        consumption_loader: ConsumptionProfileLoader,
        live_rates: LiveRateReader,
        realized_pv_loader: RealizedPvLoader,
        charge_slots: ChargeSlots,
    ) -> None:
        self._hass = hass
        self.updater = updater
        self.target_socs = target_socs
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

    # ─── Public orchestration ──────────────────────────────────────────────

    def recalculate_all(self) -> None:
        """Recalculate every forecast variant + target_soc + notify (weather refresh / init)."""
        self._recalculate_at6()
        self._recalculate_live()
        self._recalculate_tomorrow()
        self._tick_extrapolated()
        self._recalculate_target_soc_now()
        self._notify_listeners()

    # ─── HA event callbacks ────────────────────────────────────────────────

    @callback
    def on_weather_update(self) -> None:
        """Weather forecast changed — recalculate all (history+forecast affects all)."""
        self.recalculate_all()

    @callback
    def on_solcast_at6_change(self, event: Event) -> None:
        """Solcast at_6 snapshot changed — refresh AT_6 strategy + target_soc."""
        self._recalculate_at6()
        self._recalculate_target_soc_now()
        self._notify_listeners()

    @callback
    def on_solcast_live_change(self, event: Event) -> None:
        """Solcast live changed — refresh LIVE + extrap variants + target_soc."""
        self._recalculate_live()
        self._tick_extrapolated()
        self._recalculate_target_soc_now()
        self._notify_listeners()

    @callback
    def on_solcast_tomorrow_change(self, event: Event) -> None:
        """Solcast tomorrow changed — refresh TOMORROW_AT_6 + TOMORROW_LIVE + target_soc."""
        self._recalculate_tomorrow()
        self._recalculate_target_soc_now()
        self._notify_listeners()

    @callback
    def on_charge_slots_change(self) -> None:
        """Coordinator updated — `ChargeSlots.tomorrow` may have shifted.

        Catches: initial setup race (ChargeSlots empty during first
        `recalculate_all`) and runtime shift after RCE-tomorrow prices
        arrive ~14:00 and optimal pre-charge window moves.
        """
        self._refresh_inputs(dt_util.now())
        self._recalculate_target_soc_now()
        self._notify_listeners()

    @callback
    def on_minute_tick(self) -> None:
        """Per-minute cron — refresh extrap variants + target_soc."""
        self._tick_extrapolated()
        self._recalculate_target_soc_now()
        self._notify_listeners()

    # ─── Updater dispatch paths (trigger-named, delta-only) ────────────────

    def _recalculate_at6(self) -> None:
        """Refresh AT_6 via updater.today_at_6_forecast_updated.

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
        self._refresh_target_soc_inputs()
        self._push_extrap_inputs()
        self.updater.today_at_6_forecast_updated(solcast_periods, weather, now)

    def _recalculate_live(self) -> None:
        """Refresh LIVE via updater.today_live_forecast_updated."""
        solcast_periods = self._solcast.read_live()
        if not solcast_periods:
            return
        now = dt_util.now()
        weather = self._build_weather(now.date())
        self._refresh_target_soc_inputs()
        self._push_extrap_inputs()
        self.updater.today_live_forecast_updated(solcast_periods, weather, now)

    def _recalculate_tomorrow(self) -> None:
        """Refresh TOMORROW_* via updater.tomorrow_forecast_updated."""
        solcast_periods = self._solcast.read_tomorrow()
        if not solcast_periods:
            return
        now = dt_util.now()
        weather = self._build_weather(now.date())
        self._refresh_target_soc_inputs()
        self.updater.tomorrow_forecast_updated(solcast_periods, weather, now)

    def _tick_extrapolated(self) -> None:
        """Per-minute tick — refresh PV signals + extrap inputs, dispatch."""
        now = dt_util.now()
        self._refresh_target_soc_inputs()
        self._push_extrap_inputs()
        self.updater.live_pv_updated(self._build_live_signals(), now)

    # ─── TargetSoc recalc (forecast aggregate) ─────────────────────────────

    def _recalculate_target_soc_now(self) -> None:
        """Pull current updater state + recompute target_soc_*. Cheap (pure)."""
        self.target_socs.recalculate_target_soc(self.updater, dt_util.now())

    # ─── Input VO builders — read boundary, push to aggregates ─────────────

    def _build_live_signals(self) -> LivePvSignals:
        """Read PV-side live rates + return as immutable VO."""
        return LivePvSignals(
            pv_power_w=self._live_rates.read_pv_power_w(),
            bucket_so_far_kwh=self._live_rates.read_pv_bucket_so_far_kwh(),
            derivative_w_per_min=self._live_rates.read_pv_derivative_w_per_min(),
            stability_stable=self._live_rates.read_pv_stability_stable(),
        )

    def _refresh_target_soc_inputs(self) -> None:
        """Read cons-side live + pre-charge gates; push to target_socs as VO."""
        tomorrow_slot = self._charge_slots.tomorrow
        self.target_socs.refresh_inputs(
            TargetSocInputs(
                live_consumption_w=self._live_rates.read_consumption_w(),
                start_charge_hour_today=(
                    self._live_rates.read_start_charge_hour_today_override()
                ),
                start_charge_hour_tomorrow=(
                    int(tomorrow_slot.start_hour) if tomorrow_slot is not None else None
                ),
            )
        )

    def _push_extrap_inputs(self) -> None:
        """Forward cons knobs to updater for legacy EXTRAP recompute (Iter 1b).

        EXTRAP variants are unbound in Iter 1b — they read consumption_w +
        start_charge_hour from updater-cached state. Iter 3 binds them and
        this forwarding disappears.
        """
        self.updater.refresh_extrap_inputs(
            self._realized_pv_today,
            self.target_socs.inputs.live_consumption_w,
            self.target_socs.inputs.start_charge_hour_today,
        )

    # ─── Consumption profiles refresh (async I/O paths) ────────────────────

    async def refresh_realized_pv(self) -> None:
        """Fetch today's realized PV per bucket from recorder; cache for next recalc."""
        try:
            self._realized_pv_today = await self._realized_pv_loader.fetch_today(
                dt_util.now().date()
            )
        except Exception:  # noqa: BLE001 — defensive, don't crash integration
            _LOGGER.exception("Failed to fetch realized PV history")

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
            await self.target_socs.consumption_profiles.refresh_full(
                self._consumption_loader, now
            )
        except Exception:  # noqa: BLE001 — defensive, don't crash integration
            _LOGGER.exception("Failed to refresh consumption profiles (full)")
            return
        self.target_socs.recalculate_target_soc(self.updater, now)
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
            await self.target_socs.consumption_profiles.refresh_tomorrow_only(
                self._consumption_loader, now
            )
        except Exception:  # noqa: BLE001 — defensive
            _LOGGER.exception("Failed to refresh consumption profiles (tomorrow)")
            return
        self.target_socs.recalculate_target_soc(self.updater, now)
        self._notify_listeners()

    def _maybe_schedule_profile_retry(self) -> None:
        """Schedule another `refresh_profiles_full` if the entity says so.

        The entity's `should_retry()` checks both partial-state and the
        retry budget. Existing pending retry is cancelled so a fresh
        scheduled trigger (daily 05:55) doesn't stack with it.
        """
        if not self.target_socs.consumption_profiles.should_retry():
            self._cancel_profile_retry()
            return
        self._cancel_profile_retry()
        attempt = self.target_socs.consumption_profiles.failed_attempts
        _LOGGER.warning(
            "Consumption profile refresh partial (attempt %d/%d) — "
            "retrying in %.0fs",
            attempt,
            self.target_socs.consumption_profiles.MAX_RETRIES,
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

    # ─── Helpers + listener fan-out ────────────────────────────────────────

    def _build_weather(self, day: date) -> list[WeatherConditionAtHour]:
        """Combine weather history (past hours) + live forecast (future hours).

        Pure orchestration: read 2 sources + delegate merge to domain
        `merge_weather_conditions`.
        """
        history = self._weather_history.get_conditions_for_date(day)
        forecast = self._weather_listener.forecast_conditions
        return pv_forecast.merge_weather_conditions(history, forecast)

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        """Register listener for forecast/SoC change events. Returns unsubscribe fn."""

        def remove_listener() -> None:
            self._listeners.pop(remove_listener)

        self._listeners[remove_listener] = update_callback
        return remove_listener

    def _notify_listeners(self) -> None:
        for update_callback in list(self._listeners.values()):
            update_callback()
