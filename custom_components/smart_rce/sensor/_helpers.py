"""Common helpers used by multiple sensor classes."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback


def register_state_writer(sensor: SensorEntity, source: Any) -> None:
    """Register listener that invokes async_write_ha_state on each notify.

    `source` must have `async_add_listener(callback) -> Callable[[], None]`.
    Common pattern for sensors subscribed to Ems / PvForecastService.
    Eliminates 5-line duplication in `async_added_to_hass` per class.
    """

    @callback
    def listener() -> None:
        sensor.async_write_ha_state()

    remove_listener = source.async_add_listener(listener)
    setattr(remove_listener, "_hass_callback", True)
    sensor.async_on_remove(remove_listener)
