"""Non-work reader — reads the mammotion `non_work_hours` sensor → NonWorkHours.

The mammotion integration exposes the quiet window as a sensor whose state is a
12-hour string like `"08:35pm - 10:05am"`. `NonWorkReader` is the driving
adapter: it owns the hass handle and the sensor entity id (the application
layer stays unaware of both) and returns the parsed domain VO (or `None` when
unavailable / unparsable). It also owns change subscription, so the entity id
never leaks outside this module. String parsing is isolated in
`parse_non_work_state` (pure, unit-tested directly) so the read path stays a
thin hass wrapper.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import TYPE_CHECKING, Final

from custom_components.smart_rce.garden.const import LUBA_NON_WORK_SENSOR
from custom_components.smart_rce.garden.domain.non_work import NonWorkHours
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers.event import async_track_state_change_event

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import CALLBACK_TYPE, HomeAssistant

_UNAVAILABLE: Final = (STATE_UNKNOWN, STATE_UNAVAILABLE)


class NonWorkReader:
    """Reads + watches the mammotion non-work sensor (owns hass and entity id)."""

    _ENTITY_ID: Final[str] = LUBA_NON_WORK_SENSOR

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def read_non_work_hours(self) -> NonWorkHours | None:
        """Read the sensor and parse it into NonWorkHours."""
        state = self._hass.states.get(NonWorkReader._ENTITY_ID)
        return parse_non_work_state(state.state if state else None)

    def subscribe(self, on_change: Callable[[], None]) -> CALLBACK_TYPE:
        """Invoke `on_change` on every sensor state change; returns unsubscribe."""
        return async_track_state_change_event(
            self._hass, [NonWorkReader._ENTITY_ID], lambda _event: on_change()
        )


def parse_non_work_state(raw: str | None) -> NonWorkHours | None:
    """Parse the raw sensor state string into NonWorkHours (pure)."""
    if not raw or raw in _UNAVAILABLE:
        return None
    parts = raw.split("-")
    if len(parts) != 2:
        return None
    try:
        return NonWorkHours(start=_parse_12h(parts[0]), end=_parse_12h(parts[1]))
    except ValueError:
        return None


def _parse_12h(token: str) -> time:
    return datetime.strptime(token.strip().upper(), "%I:%M%p").time()  # noqa: DTZ007
