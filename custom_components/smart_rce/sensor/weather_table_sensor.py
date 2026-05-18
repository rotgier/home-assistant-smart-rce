"""SmartRceWeatherTable sensors — back the dashboard weather table cards.

Two variants:

1. `SmartRceWeatherTableSensor` (live) — reflects current state. Refreshes on
   every wetteronline forecast change + date picker change.
2. `SmartRceWeatherTableSnapshotSensor` — reconstructs the table "as it
   was" at a chosen historic moment. Subscribes to 3 helpers (date picker
   + preset + custom time) and falls back to live mode when preset == EOD
   or target_date is in the future.

Both expose `rows` as a state attribute for the markdown card to iterate
via `state_attr('sensor.X', 'rows')`. Recompute is async (recorder query) —
drive it via `async_create_task(...)` and write state when ready.
"""

from __future__ import annotations

from datetime import date, datetime, time
import logging
from typing import Any, Final

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from ..application.weather_table_service import WeatherTableService
from ..const import DOMAIN
from ..coordinator import SmartRceDataUpdateCoordinator
from ..infrastructure.weather_listener import WeatherForecastListener

UNIQUE_ID_LIVE: Final = f"{DOMAIN}_weather_table"
UNIQUE_ID_SNAPSHOT: Final = f"{DOMAIN}_weather_table_snapshot"
DATE_PICKER_ENTITY_ID: Final = "input_datetime.energy_chart_date"
SNAPSHOT_PRESET_ENTITY_ID: Final = "input_select.rce_forecast_snapshot_preset"
SNAPSHOT_CUSTOM_ENTITY_ID: Final = "input_datetime.rce_forecast_snapshot_custom"

_LOGGER = logging.getLogger(__name__)


class _WeatherTableSensorBase(SensorEntity):
    """Shared scaffolding: recompute pipeline + recorder result publishing.

    Subclasses provide:
    - `_attr_name`, `_attr_unique_id`
    - `_resolve_inputs()` returning `(target_date, snapshot_time | None)`
    - `_subscribe()` wiring the relevant state-change triggers
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        service: WeatherTableService,
        weather_listener: WeatherForecastListener,
        rce_coordinator: SmartRceDataUpdateCoordinator,
    ) -> None:
        self._hass = hass
        self._service = service
        self._weather_listener = weather_listener
        self._attr_device_info = rce_coordinator.device_info
        self._rows: list[dict[str, Any]] = []
        self._date: str | None = None
        self._snapshot_time: str | None = None
        self._last_compute_at: str | None = None

    @property
    def native_value(self) -> str | None:
        return self._last_compute_at

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "date": self._date,
            "rows": self._rows,
            "snapshot_time": self._snapshot_time,
            "last_compute_at": self._last_compute_at,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._subscribe()
        self._schedule_recompute()
        _LOGGER.debug(
            "Setup of %s sensor %s (unique_id: %s)",
            type(self).__name__,
            self.entity_id,
            self._attr_unique_id,
        )

    def _subscribe(self) -> None:
        raise NotImplementedError

    def _resolve_inputs(self) -> tuple[date, datetime | None]:
        raise NotImplementedError

    @callback
    def _schedule_recompute(self) -> None:
        self._hass.async_create_task(self._recompute())

    async def _recompute(self) -> None:
        target_date, snapshot_time = self._resolve_inputs()
        try:
            result = await self._service.async_get_table(
                target_date, snapshot_time=snapshot_time
            )
        except Exception:  # noqa: BLE001 — defensive, never crash the entity
            _LOGGER.exception(
                "WeatherTable recompute failed for %s (snapshot=%s)",
                target_date,
                snapshot_time,
            )
            return
        self._date = result["date"]
        self._rows = result["rows"]
        self._snapshot_time = result.get("snapshot_time")
        self._last_compute_at = dt_util.now().isoformat()
        self.async_write_ha_state()


class SmartRceWeatherTableSensor(_WeatherTableSensorBase):
    """Live weather table — current forecast + history up to now."""

    _attr_name = "Weather Table"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._attr_unique_id = UNIQUE_ID_LIVE

    def _subscribe(self) -> None:
        # Weather updates — listener fires on each wetteronline forecast change.
        remove_weather = self._weather_listener.async_add_listener(
            self._on_weather_update
        )
        setattr(remove_weather, "_hass_callback", True)
        self.async_on_remove(remove_weather)

        # Date picker changes.
        self.async_on_remove(
            async_track_state_change_event(
                self._hass, [DATE_PICKER_ENTITY_ID], self._on_state_change
            )
        )

    @callback
    def _on_weather_update(self) -> None:
        self._schedule_recompute()

    @callback
    def _on_state_change(self, _event: Event[EventStateChangedData]) -> None:
        self._schedule_recompute()

    def _resolve_inputs(self) -> tuple[date, datetime | None]:
        return _read_target_date(self._hass), None


class SmartRceWeatherTableSnapshotSensor(_WeatherTableSensorBase):
    """Snapshot weather table — reconstructs state at a chosen historic moment.

    Snapshot resolved from 3 helpers:
    - `input_datetime.energy_chart_date` — which day to view
    - `input_select.rce_forecast_snapshot_preset` — preset hour (or "EOD"/"Custom")
    - `input_datetime.rce_forecast_snapshot_custom` — custom time (when preset=Custom)

    Live mode (snapshot_time=None) when:
    - preset == "EOD" AND target_date >= today (no historic moment to project to)
    - target_date > today (future date — no history exists yet)
    """

    _attr_name = "Weather Table Snapshot"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._attr_unique_id = UNIQUE_ID_SNAPSHOT

    def _subscribe(self) -> None:
        # Snapshot doesn't subscribe to weather listener — its inputs are
        # the helpers + (implicitly) recorder history at snapshot_time.
        self.async_on_remove(
            async_track_state_change_event(
                self._hass,
                [
                    DATE_PICKER_ENTITY_ID,
                    SNAPSHOT_PRESET_ENTITY_ID,
                    SNAPSHOT_CUSTOM_ENTITY_ID,
                ],
                self._on_state_change,
            )
        )

    @callback
    def _on_state_change(self, _event: Event[EventStateChangedData]) -> None:
        self._schedule_recompute()

    def _resolve_inputs(self) -> tuple[date, datetime | None]:
        target_date = _read_target_date(self._hass)
        today = dt_util.now().date()

        # Future date — no history, fall back to live.
        if target_date > today:
            return target_date, None

        preset = self._read_preset()
        if preset == "EOD":
            if target_date >= today:
                # Today's EOD = live.
                return target_date, None
            # Past day EOD = end of that day.
            tz = dt_util.DEFAULT_TIME_ZONE
            return target_date, datetime.combine(
                target_date, time(23, 59, 59), tzinfo=tz
            )

        hhmmss = self._read_snapshot_hhmmss(preset)
        if hhmmss is None:
            return target_date, None
        tz = dt_util.DEFAULT_TIME_ZONE
        snapshot_time = datetime.combine(target_date, hhmmss, tzinfo=tz)
        return target_date, snapshot_time

    def _read_preset(self) -> str:
        state = self._hass.states.get(SNAPSHOT_PRESET_ENTITY_ID)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return "EOD"
        return state.state

    def _read_snapshot_hhmmss(self, preset: str) -> time | None:
        """Resolve preset → time. 'Custom' reads the input_datetime helper."""
        if preset == "Custom":
            state = self._hass.states.get(SNAPSHOT_CUSTOM_ENTITY_ID)
            if state is None or state.state in ("unknown", "unavailable", ""):
                return None
            try:
                return time.fromisoformat(state.state)
            except ValueError:
                return None
        # Preset is "HH:MM" like "06:11", "08:41".
        try:
            return time.fromisoformat(preset)
        except ValueError:
            return None


def _read_target_date(hass: HomeAssistant) -> date:
    state = hass.states.get(DATE_PICKER_ENTITY_ID)
    if state and state.state not in ("unknown", "unavailable", ""):
        try:
            return datetime.fromisoformat(state.state).date()
        except ValueError:
            pass
    return dt_util.now().date()
