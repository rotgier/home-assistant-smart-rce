"""StateWriterMixin — listener-driven state refresh for sensor entities.

Subscribe to a domain source (Ems / PvForecastService / WeatherForecastListener)
and invoke `async_write_ha_state` on each notify. Eliminates duplication of
listener wiring boilerplate in `async_added_to_hass` per sensor class.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback


class StateWriterMixin(SensorEntity):
    """Mixin providing `_register_state_writer(source)` for sensor classes.

    Source must expose `async_add_listener(callback) -> Callable[[], None]`.
    The returned `remove_listener` is registered via `async_on_remove` so HA
    cleans it up on entity removal.
    """

    def _register_state_writer(self, source: Any) -> None:
        @callback
        def listener() -> None:
            self.async_write_ha_state()

        remove_listener = source.async_add_listener(listener)
        setattr(remove_listener, "_hass_callback", True)
        self.async_on_remove(remove_listener)
