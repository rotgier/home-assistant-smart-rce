"""PV Forecast Coordinator — orchestrates weather-adjusted PV estimates."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
import logging
from typing import Any, Final

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util

from .domain.pv_forecast import (
    AdjustedPvForecast,
    ConsumptionProfile,
    SolcastPeriod,
    TargetSocResult,
    WeatherConditionAtHour,
    adjust_pv_forecast_at6,
    adjust_pv_forecast_live,
    calculate_target_soc,
)
from .weather_forecast_history import WeatherForecastHistory
from .weather_listener import WeatherListenerCoordinator

SOLCAST_AT_6_ENTITY: Final = "sensor.solcast_forecast_at_6"
SOLCAST_LIVE_ENTITY: Final = "sensor.solcast_pv_forecast_prognoza_na_dzisiaj"
SOLCAST_TOMORROW_ENTITY: Final = "sensor.solcast_pv_forecast_prognoza_na_jutro"

CONSUMPTION_SENSOR_ID: Final = "sensor.total_consumption_minus_bi_hourly"
PREV_DAYS_COUNT: Final = 3

_LOGGER = logging.getLogger(__name__)


def _parse_solcast_forecast(
    forecast_attr: list[dict[str, Any]],
) -> list[SolcastPeriod]:
    """Parse Solcast forecast attribute into domain objects."""
    return [
        SolcastPeriod(
            period_start=str(item["period_start"]),
            pv_estimate=item["pv_estimate"],
            pv_estimate10=item["pv_estimate10"],
            pv_estimate90=item["pv_estimate90"],
        )
        for item in forecast_attr
    ]


def _parse_weather_conditions(
    forecast_hourly: list[dict[str, Any]] | None,
) -> list[WeatherConditionAtHour]:
    """Parse WeatherListenerCoordinator.forecast_hourly into domain objects.

    Returns conditions with both hour and date, to allow matching
    against the correct day in Solcast forecast.
    """
    if not forecast_hourly:
        return []
    return [
        WeatherConditionAtHour(
            hour=datetime.fromisoformat(item["datetime"]).hour,
            condition_custom=item.get("condition_custom", "cloudy"),
            forecast_date=datetime.fromisoformat(item["datetime"]).date(),
        )
        for item in forecast_hourly
        if "datetime" in item
    ]


class PvForecastCoordinator:
    """Coordinates weather-adjusted PV forecast calculation."""

    def __init__(
        self,
        hass: HomeAssistant,
        weather_coordinator: WeatherListenerCoordinator,
        weather_forecast_history: WeatherForecastHistory,
    ) -> None:
        self._hass = hass
        self._weather_coordinator = weather_coordinator
        self._weather_forecast_history = weather_forecast_history
        self._listeners: dict[CALLBACK_TYPE, CALLBACK_TYPE] = {}
        self._cancel_solcast_listeners: list[CALLBACK_TYPE] = []
        self._cancel_profile_refresh: CALLBACK_TYPE | None = None

        self.adjusted_at_6: AdjustedPvForecast | None = None
        self.adjusted_live: AdjustedPvForecast | None = None
        self.adjusted_tomorrow: AdjustedPvForecast | None = None
        self.adjusted_tomorrow_live: AdjustedPvForecast | None = None
        self.target_soc: TargetSocResult | None = None
        self.target_soc_live: TargetSocResult | None = None
        self.target_soc_tomorrow: TargetSocResult | None = None
        self.target_soc_tomorrow_live: TargetSocResult | None = None

        # Prev-workday consumption profile instrumentation (Etap A)
        self.consumption_profiles: list[ConsumptionProfile | None] = [
            None
        ] * PREV_DAYS_COUNT
        self.target_soc_prev_days: list[TargetSocResult | None] = [
            None
        ] * PREV_DAYS_COUNT
        self.target_soc_tomorrow_prev_days: list[TargetSocResult | None] = [
            None
        ] * PREV_DAYS_COUNT
        self.target_soc_max: int | None = None
        self.target_soc_tomorrow_max: int | None = None

    async def async_start(self) -> None:
        """Start listening for weather and Solcast changes."""
        # Listen to weather changes via existing WeatherListenerCoordinator
        self._weather_coordinator.async_add_listener(self._on_weather_update)

        # Listen to Solcast entity state changes
        cancel_at6 = async_track_state_change_event(
            self._hass, [SOLCAST_AT_6_ENTITY], self._on_solcast_at6_change
        )
        cancel_live = async_track_state_change_event(
            self._hass, [SOLCAST_LIVE_ENTITY], self._on_solcast_live_change
        )
        cancel_tomorrow = async_track_state_change_event(
            self._hass, [SOLCAST_TOMORROW_ENTITY], self._on_solcast_tomorrow_change
        )
        self._cancel_solcast_listeners = [cancel_at6, cancel_live, cancel_tomorrow]

        # Daily prev-workday consumption profile refresh at 05:55 local.
        self._cancel_profile_refresh = async_track_time_change(
            self._hass, self._on_daily_profile_refresh, hour=5, minute=55, second=0
        )

        # Initial calculation
        self._recalculate_all()
        # Initial profile fetch (runs in background; _recalculate_target_soc will
        # re-run once profiles arrive).
        self._hass.async_create_task(self._refresh_profiles())

    def async_stop(self) -> None:
        """Stop listening."""
        for cancel in self._cancel_solcast_listeners:
            cancel()
        self._cancel_solcast_listeners = []
        if self._cancel_profile_refresh:
            self._cancel_profile_refresh()
            self._cancel_profile_refresh = None

    @callback
    def _on_weather_update(self) -> None:
        """Weather forecast changed — recalculate both."""
        _LOGGER.debug("Weather update received, recalculating PV forecasts")
        self._recalculate_all()

    @callback
    def _on_solcast_at6_change(self, event: Event) -> None:
        """Solcast at_6 snapshot changed — recalculate at_6."""
        _LOGGER.debug("Solcast at_6 changed, recalculating")
        self._recalculate_at6()
        self._recalculate_target_soc()
        self._notify_listeners()

    @callback
    def _on_solcast_live_change(self, event: Event) -> None:
        """Solcast live changed — recalculate live."""
        _LOGGER.debug("Solcast live changed, recalculating")
        self._recalculate_live()
        self._recalculate_target_soc()
        self._notify_listeners()

    @callback
    def _on_solcast_tomorrow_change(self, event: Event) -> None:
        """Solcast tomorrow changed — recalculate tomorrow."""
        _LOGGER.debug("Solcast tomorrow changed, recalculating")
        self._recalculate_tomorrow()
        # target_soc_tomorrow depends on adjusted_tomorrow — must also refresh.
        self._recalculate_target_soc()
        self._notify_listeners()

    def _recalculate_all(self) -> None:
        """Recalculate all forecasts and target SOC."""
        self._recalculate_at6()
        self._recalculate_live()
        self._recalculate_tomorrow()
        self._recalculate_target_soc()
        self._notify_listeners()

    def _recalculate_at6(self) -> None:
        """Recalculate weather-adjusted forecast.

        Before 6:01 — use live Solcast (has forecast fetched at 22:00).
        After 6:01 — use at_6 snapshot (fresh for today).
        """
        from homeassistant.util.dt import now as now_local

        now = now_local()
        if now.hour < 6 or (now.hour == 6 and now.minute < 2):
            entity_id = SOLCAST_LIVE_ENTITY
            attr_name = "detailedForecast"
            source = "live (pre-6:01)"
        else:
            entity_id = SOLCAST_AT_6_ENTITY
            attr_name = "forecast"
            source = "at_6"

        solcast_periods = self._read_solcast_entity(entity_id, attr_name)
        if not solcast_periods:
            return

        weather = self._build_weather_conditions(now.date())
        self.adjusted_at_6 = adjust_pv_forecast_at6(solcast_periods, weather)
        _LOGGER.debug(
            "Adjusted at_6 (source: %s): %.1f kWh (from %d periods, %d weather conditions)",
            source,
            self.adjusted_at_6.total_kwh,
            len(self.adjusted_at_6.forecast),
            len(weather),
        )

    def _recalculate_live(self) -> None:
        """Recalculate weather-adjusted forecast from live Solcast."""
        solcast_periods = self._read_solcast_entity(
            SOLCAST_LIVE_ENTITY, "detailedForecast"
        )
        if not solcast_periods:
            return

        from homeassistant.util.dt import now as now_local

        today = now_local().date()
        weather = self._build_weather_conditions(today)
        from homeassistant.util.dt import now as now_local

        self.adjusted_live = adjust_pv_forecast_live(
            solcast_periods, weather, now_local()
        )
        _LOGGER.debug(
            "Adjusted live: %.1f kWh (from %d periods)",
            self.adjusted_live.total_kwh,
            len(self.adjusted_live.forecast),
        )

    def _recalculate_target_soc(self) -> None:
        """Calculate target battery SOC from adjusted forecasts."""
        now = dt_util.now()

        if self.adjusted_at_6:
            self.target_soc = calculate_target_soc(self.adjusted_at_6, now=now)
            _LOGGER.debug("Target SOC (at_6): %d%%", self.target_soc.value)

        if self.adjusted_live:
            self.target_soc_live = calculate_target_soc(self.adjusted_live, now=now)
            _LOGGER.debug("Target SOC (live): %d%%", self.target_soc_live.value)

        # Tomorrow: always full 7-13 window.
        # Two variants with DIFFERENT adjustment semantics:
        #   target_soc_tomorrow      — AT6 modifiers (pessimistic, cloudy cap)
        #   target_soc_tomorrow_live — LIVE modifiers (optimistic, no cap)
        # The _live variant matches the adjustment used by target_soc_live
        # for today — so at midnight rollover, yesterday's target_soc_tomorrow_live
        # is numerically comparable to today's target_soc_live (both LIVE mods
        # on same Solcast forecast → continuity).
        if self.adjusted_tomorrow:
            self.target_soc_tomorrow = calculate_target_soc(self.adjusted_tomorrow)
            _LOGGER.debug("Target SOC (tomorrow): %d%%", self.target_soc_tomorrow.value)
        if self.adjusted_tomorrow_live:
            self.target_soc_tomorrow_live = calculate_target_soc(
                self.adjusted_tomorrow_live
            )
            _LOGGER.debug(
                "Target SOC (tomorrow_live): %d%%", self.target_soc_tomorrow_live.value
            )

        # Prev-workday instrumentation (Etap A).
        # Uses adjusted_live for today + adjusted_tomorrow_live for tomorrow,
        # combined with consumption profiles from N workdays back.
        for i, profile in enumerate(self.consumption_profiles):
            if self.adjusted_live and profile is not None:
                self.target_soc_prev_days[i] = calculate_target_soc(
                    self.adjusted_live,
                    consumption_profile=profile,
                    now=now,
                )
            else:
                self.target_soc_prev_days[i] = None

            if self.adjusted_tomorrow_live and profile is not None:
                self.target_soc_tomorrow_prev_days[i] = calculate_target_soc(
                    self.adjusted_tomorrow_live,
                    consumption_profile=profile,
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
        _LOGGER.debug(
            "Target SOC max: today=%s tomorrow=%s",
            self.target_soc_max,
            self.target_soc_tomorrow_max,
        )

    @callback
    def _on_daily_profile_refresh(self, _now: datetime) -> None:
        """Scheduled at 05:55 local — refresh prev-workday consumption profiles."""
        self._hass.async_create_task(self._refresh_profiles())

    async def _refresh_profiles(self) -> None:
        """Fetch profiles + recalc target SOC + notify listeners."""
        try:
            self.consumption_profiles = await self._fetch_all_consumption_profiles()
        except Exception:  # noqa: BLE001 — defensive, don't crash integration
            _LOGGER.exception("Failed to fetch consumption profiles")
            return
        self._recalculate_target_soc()
        self._notify_listeners()

    async def _fetch_all_consumption_profiles(
        self,
    ) -> list[ConsumptionProfile | None]:
        """Fetch PREV_DAYS_COUNT prev-workday profiles in a SINGLE LTS query.

        Walk back N workdays (skip weekends), compute earliest..latest date span,
        fetch 5-min stats once, then bucket per date in memory.
        """
        dates: list[date | None] = [
            self._walk_back_workdays(i + 1) for i in range(PREV_DAYS_COUNT)
        ]
        valid_dates = [d for d in dates if d is not None]
        if not valid_dates:
            _LOGGER.debug("No valid prev workdays found")
            return [None] * PREV_DAYS_COUNT

        tz = dt_util.DEFAULT_TIME_ZONE
        earliest, latest = min(valid_dates), max(valid_dates)
        start = datetime.combine(earliest, time(6, 30), tzinfo=tz)
        end = datetime.combine(latest, time(13, 35), tzinfo=tz)

        instance = get_instance(self._hass)
        stats = await instance.async_add_executor_job(
            statistics_during_period,
            self._hass,
            start,
            end,
            {CONSUMPTION_SENSOR_ID},
            "5minute",
            None,
            {"state"},
        )
        slots = stats.get(CONSUMPTION_SENSOR_ID, [])
        _LOGGER.debug(
            "Fetched %d 5-min slots for %s between %s and %s",
            len(slots),
            CONSUMPTION_SENSOR_ID,
            start.date(),
            end.date(),
        )

        # Utility_meter resetuje na :00 i :30 — last pre-reset slot to :25 i :55.
        # Value state in that slot = total consumption w 30-min cyklu.
        # Bucket (hour, 0)  = state w slocie (hour, 25)
        # Bucket (hour, 30) = state w slocie (hour, 55)
        by_date: dict[date, dict[tuple[int, int], float]] = {d: {} for d in valid_dates}
        for slot in slots:
            raw_start = slot.get("start")
            if raw_start is None:
                continue
            ts = datetime.fromtimestamp(float(raw_start), tz=UTC).astimezone(tz)
            d = ts.date()
            if d not in by_date or ts.hour < 7 or ts.hour >= 13:
                continue
            state_val = slot.get("state")
            if state_val is None:
                continue
            try:
                value = float(state_val)
            except (TypeError, ValueError):
                continue
            if ts.minute == 25:
                by_date[d][(ts.hour, 0)] = value
            elif ts.minute == 55:
                by_date[d][(ts.hour, 30)] = value

        return [
            ConsumptionProfile(buckets=dict(by_date[d]), source_date=d)
            if d and by_date.get(d)
            else None
            for d in dates
        ]

    def _walk_back_workdays(self, days_back: int) -> date | None:
        """Return date N workdays ago (skip weekends).

        TODO Etap E: replace heuristic with binary_sensor.workday_sensor (PL holidays).
        """
        today = dt_util.now().date()
        target = today
        found = 0
        while found < days_back:
            target -= timedelta(days=1)
            if target.weekday() < 5:
                found += 1
            if (today - target).days > 14:  # safety break
                return None
        return target

    def _recalculate_tomorrow(self) -> None:
        """Recalculate weather-adjusted forecast for tomorrow — two variants.

        `adjusted_tomorrow`      — AT6 modifiers (pessimistic, cloudy cap @ hour 7).
                                   Used for wieczorne planowanie: safety lower-bound
                                   how much battery we'll need tomorrow morning.
        `adjusted_tomorrow_live` — LIVE modifiers (optimistic, no cap).
                                   Used after midnight rollover comparison: aligns
                                   with tomorrow's `adjusted_live` for continuity.
        """
        solcast_periods = self._read_solcast_entity(
            SOLCAST_TOMORROW_ENTITY, "detailedForecast"
        )
        if not solcast_periods:
            return

        from datetime import timedelta

        from homeassistant.util.dt import now as now_local

        now = now_local()
        tomorrow = (now + timedelta(days=1)).date()
        weather = self._build_weather_conditions(tomorrow)
        self.adjusted_tomorrow = adjust_pv_forecast_at6(solcast_periods, weather)
        # adjust_pv_forecast_live checks is_first_hour = (period.hour == now.hour).
        # For tomorrow's periods (date = tomorrow), no match → all periods use
        # standard LIVE modifiers (no special first-hour treatment).
        self.adjusted_tomorrow_live = adjust_pv_forecast_live(
            solcast_periods, weather, now
        )
        _LOGGER.debug(
            "Adjusted tomorrow: AT6=%.1f kWh, LIVE=%.1f kWh (from %d periods, %d weather conditions)",
            self.adjusted_tomorrow.total_kwh,
            self.adjusted_tomorrow_live.total_kwh,
            len(self.adjusted_tomorrow.forecast),
            len(weather),
        )

    def _build_weather_conditions(
        self, today: date | None = None
    ) -> list[WeatherConditionAtHour]:
        """Build weather conditions from history (past hours) + forecast (future hours).

        History has conditions for hours that already passed today.
        Forecast has conditions for upcoming hours (possibly multiple days).
        Both have forecast_date for correct matching.
        """
        # From history tracker (past hours today, already "frozen")
        history_conditions: list[WeatherConditionAtHour] = []
        if today:
            history_conditions = self._weather_forecast_history.get_conditions_for_date(
                today
            )

        # From live forecast (future hours, with date filtering via forecast_date)
        forecast_conditions = _parse_weather_conditions(
            self._weather_coordinator.forecast_hourly
        )

        # Combine: history first, forecast overwrites (forecast is more recent for future hours)
        combined: dict[tuple[date, int], WeatherConditionAtHour] = {}
        for c in history_conditions:
            if c.forecast_date:
                combined[(c.forecast_date, c.hour)] = c
        for c in forecast_conditions:
            if c.forecast_date:
                combined[(c.forecast_date, c.hour)] = c

        return list(combined.values())

    def _read_solcast_entity(
        self, entity_id: str, attr_name: str
    ) -> list[SolcastPeriod] | None:
        """Read Solcast forecast from HA state machine."""
        state = self._hass.states.get(entity_id)
        if not state:
            _LOGGER.debug("Entity %s not found", entity_id)
            return None

        forecast_attr = state.attributes.get(attr_name)
        if not forecast_attr:
            _LOGGER.debug("Entity %s has no attribute %s", entity_id, attr_name)
            return None

        return _parse_solcast_forecast(forecast_attr)

    # --- Listener pattern (same as Ems) ---

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        def remove_listener() -> None:
            self._listeners.pop(remove_listener)

        self._listeners[remove_listener] = update_callback
        return remove_listener

    def _notify_listeners(self) -> None:
        for update_callback in list(self._listeners.values()):
            update_callback()
