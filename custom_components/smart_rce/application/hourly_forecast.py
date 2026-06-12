"""Published port: hourly weather forecast exposed by the ems context.

Cross-context integration contract (ADR-024): garden consumes the wetteronline
hourly forecast that ems already maintains. The Protocol lives HERE — in the
application layer of the context that owns the data — and the composition
roots integrate at the factory level: `async_setup_entry` passes ems's
`WeatherForecastListener` (which satisfies this Protocol structurally) into
`create_garden`. Garden never imports ems infrastructure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import CALLBACK_TYPE
    from homeassistant.util.json import JsonValueType


class HourlyForecastProvider(Protocol):
    """Raw hourly forecast (HA weather shape) + change subscription."""

    forecast_hourly: list[JsonValueType] | None

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        """Invoke `update_callback` on forecast updates; returns unsubscribe."""
        ...
