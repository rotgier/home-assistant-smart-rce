"""LiveRateReader — driving adapter for real-time PV/consumption rates.

Reads 5-minute averaged power sensors from HA states. Used by PvForecastService
to extrapolate the in-progress 30-min bucket using actual current rates instead
of the (forecast × remaining_fraction) prorate.

Returns None when sensors are unavailable so the caller can suppress 5-min
extrapolated outputs (sensors emit 'unknown' instead of stale or fabricated data).

Hexagonal pattern: **driving adapter (inbound)** — domain dictates "give me
the current PV power and consumption", the concrete impl reads HA states.
"""

from __future__ import annotations

import logging
from typing import Final

from homeassistant.core import HomeAssistant

_PV_POWER_5MIN_ENTITY: Final = "sensor.pv_power_avg_5_minutes"
_CONSUMPTION_5MIN_ENTITY: Final = "sensor.house_consumption_avg_5_minutes"

_LOGGER = logging.getLogger(__name__)


class LiveRateReader:
    """Reads 5-min averaged PV power + consumption (W) from HA states."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def read_pv_power_w(self) -> float | None:
        return self._read_w(_PV_POWER_5MIN_ENTITY)

    def read_consumption_w(self) -> float | None:
        return self._read_w(_CONSUMPTION_5MIN_ENTITY)

    def _read_w(self, entity_id: str) -> float | None:
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None
