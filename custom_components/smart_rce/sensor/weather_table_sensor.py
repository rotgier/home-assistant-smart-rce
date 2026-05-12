"""SmartRceWeatherTableSensor — backs the dashboard weather table card.

Wraps `WeatherTableService.async_get_table()` as a state-bearing entity
so a markdown card can iterate `state_attr('sensor.smart_rce_weather_table',
'rows')` from Jinja. Trigger sources:

1. `WeatherForecastListener.async_add_listener(...)` — fires on every
   wetteronline forecast attribute change (covers all coordinator
   refreshes via the listener's diff check).
2. `async_track_state_change_event` on `input_datetime.energy_chart_date`
   — the dashboard's date picker.

Recompute is async (recorder query) — drive it via
`async_create_task(...)` and write state when ready. The listener
already deduplicates "no change" events so no extra debouncer is needed.
"""

from __future__ import annotations

from datetime import date, datetime
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

UNIQUE_ID: Final = f"{DOMAIN}_weather_table"
DATE_PICKER_ENTITY_ID: Final = "input_datetime.energy_chart_date"

_LOGGER = logging.getLogger(__name__)


class SmartRceWeatherTableSensor(SensorEntity):
    """Sensor whose `rows` attribute is the assembled weather table."""

    _attr_has_entity_name = True
    _attr_name = "Weather Table"
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
        self._attr_unique_id = UNIQUE_ID
        self._attr_device_info = rce_coordinator.device_info
        self._rows: list[dict[str, Any]] = []
        self._date: str | None = None
        self._last_compute_at: str | None = None

    @property
    def native_value(self) -> str | None:
        return self._last_compute_at

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "date": self._date,
            "rows": self._rows,
            "last_compute_at": self._last_compute_at,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # 1. Weather updates — listener fires on each wetteronline forecast change.
        remove_weather = self._weather_listener.async_add_listener(
            self._on_weather_update
        )
        setattr(remove_weather, "_hass_callback", True)
        self.async_on_remove(remove_weather)

        # 2. Date picker changes.
        self.async_on_remove(
            async_track_state_change_event(
                self._hass, [DATE_PICKER_ENTITY_ID], self._on_date_change
            )
        )

        # Initial compute on add.
        self._schedule_recompute()
        _LOGGER.debug(
            "Setup of Weather Table sensor %s (unique_id: %s)",
            self.entity_id,
            self._attr_unique_id,
        )

    @callback
    def _on_weather_update(self) -> None:
        self._schedule_recompute()

    @callback
    def _on_date_change(self, _event: Event[EventStateChangedData]) -> None:
        self._schedule_recompute()

    @callback
    def _schedule_recompute(self) -> None:
        self._hass.async_create_task(self._recompute())

    async def _recompute(self) -> None:
        target_date = self._read_target_date()
        try:
            result = await self._service.async_get_table(target_date)
        except Exception:  # noqa: BLE001 — defensive, never crash the entity
            _LOGGER.exception("WeatherTable recompute failed for %s", target_date)
            return
        self._date = result["date"]
        self._rows = result["rows"]
        self._last_compute_at = dt_util.now().isoformat()
        self.async_write_ha_state()

    def _read_target_date(self) -> date:
        state = self._hass.states.get(DATE_PICKER_ENTITY_ID)
        if state and state.state not in ("unknown", "unavailable", ""):
            try:
                return datetime.fromisoformat(state.state).date()
            except ValueError:
                pass
        return dt_util.now().date()
