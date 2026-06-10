"""Non-work reader — reads the mammotion `non_work_hours` sensor → NonWorkHours.

The mammotion integration exposes the quiet window as a sensor whose state is a
12-hour string like `"08:35pm - 10:05am"`. This driving adapter reads that
entity from hass and returns the parsed domain VO (or `None` when unavailable /
unparsable). String parsing is isolated in `parse_non_work_state` (pure,
unit-tested directly) so the read path stays a thin hass wrapper.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import TYPE_CHECKING, Final

from custom_components.smart_rce.garden.domain.non_work import NonWorkHours
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_UNAVAILABLE: Final = (STATE_UNKNOWN, STATE_UNAVAILABLE)


def read_non_work_hours(hass: HomeAssistant, entity_id: str) -> NonWorkHours | None:
    """Read the mammotion non_work_hours sensor and parse it into NonWorkHours."""
    state = hass.states.get(entity_id)
    return parse_non_work_state(state.state if state else None)


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
