"""WeatherForecastHistorySensor — hourly weather forecast conditions tracker."""

from __future__ import annotations

import logging
from typing import Any, Final

from homeassistant.components.sensor import RestoreSensor
from homeassistant.util.dt import now as now_local

from ..const import DOMAIN
from ..coordinator import SmartRceDataUpdateCoordinator
from ..domain.weather_forecast_history import WeatherForecastHistory
from ._state_writer_mixin import StateWriterMixin

UNIQUE_ID_PREFIX: Final = DOMAIN

_LOGGER = logging.getLogger(__name__)


class WeatherForecastHistorySensor(StateWriterMixin, RestoreSensor):
    """Sensor showing current-hour weather condition (e.g. '07:00 cloudy').

    State changes once per hour. Attribute 'hours' is the full-day snapshot
    (24 hour-keyed conditions) updated every ~5 min from wetteronline forecast.

    Recorder saves ~30 entries/day: 24 state changes (hourly) + ~6 attribute-only
    changes when WetterOnline updates forecast for future hours.
    To get the forecast snapshot from the start of hour X, take the FIRST
    recorder entry in that hour (see ADR 013).
    """

    _attr_has_entity_name = True
    _attr_name = "Weather Forecast History"

    def __init__(
        self,
        weather_history: WeatherForecastHistory,
        weather_listener: Any,
        rce_coordinator: SmartRceDataUpdateCoordinator,
    ) -> None:
        self._weather_history = weather_history
        self._weather_listener = weather_listener
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_weather_forecast_history"
        self._attr_device_info = rce_coordinator.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Restore aggregate state — `hours` attribute carries the full
        # per-hour condition map; `current_hour_label(now)` falls back to
        # 'cloudy' if a slot is missing, so this restore avoids 'unknown'
        # state immediately after reload.
        last_state = await self.async_get_last_state()
        if last_state:
            hours_attr = last_state.attributes.get("hours")
            if hours_attr:
                self._weather_history.restore(hours_attr, now_local().date())

        self._register_state_writer(self._weather_listener)
        _LOGGER.debug(
            "Setup of Weather Forecast History sensor %s (unique_id: %s)",
            self.entity_id,
            self._attr_unique_id,
        )

    @property
    def native_value(self) -> str | None:
        return self._weather_history.current_hour_label(now_local())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"hours": self._weather_history.hours_attribute}
