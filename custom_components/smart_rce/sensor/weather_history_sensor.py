"""WeatherForecastHistorySensor — hourly weather forecast conditions tracker."""

from __future__ import annotations

import logging
from typing import Any, Final

from homeassistant.components.sensor import RestoreSensor
from homeassistant.core import callback
from homeassistant.util.dt import now as now_local

from ..const import DOMAIN
from ..coordinator import SmartRceDataUpdateCoordinator
from ..domain.weather_forecast_history import WeatherForecastHistory

UNIQUE_ID_PREFIX: Final = DOMAIN

_LOGGER = logging.getLogger(__name__)


class WeatherForecastHistorySensor(RestoreSensor):
    """Sensor tracking hourly weather forecast conditions throughout the day.

    State changes once per hour (e.g. "07:00 cloudy").
    Attribute 'hours' updates every ~5 min from wetteronline forecast.

    Recorder saves ~30 entries/day: 24 state changes (hourly) + ~6 attribute-only
    changes (when WetterOnline updates forecast for future hours).
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
        self._attr_native_value: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Restore from RestoreSensor.
        last_sensor_data = await self.async_get_last_sensor_data()
        if last_sensor_data and last_sensor_data.native_value:
            self._attr_native_value = last_sensor_data.native_value

        last_state = await self.async_get_last_state()
        if last_state:
            hours_attr = last_state.attributes.get("hours")
            if hours_attr:
                self._weather_history.restore(hours_attr, now_local().date())

        @callback
        def on_weather_update() -> None:
            self._handle_weather_update()

        remove_listener = self._weather_listener.async_add_listener(on_weather_update)
        setattr(remove_listener, "_hass_callback", True)
        self.async_on_remove(remove_listener)

        _LOGGER.debug(
            "Setup of Weather Forecast History sensor %s (unique_id: %s)",
            self.entity_id,
            self._attr_unique_id,
        )

    @callback
    def _handle_weather_update(self) -> None:
        """Refresh sensor state — write side managed by factory."""
        self._attr_native_value = self._weather_history.current_hour_label(now_local())
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        return self._attr_native_value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"hours": self._weather_history.hours_attribute}
