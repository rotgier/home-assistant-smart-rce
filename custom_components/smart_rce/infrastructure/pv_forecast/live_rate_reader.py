"""LiveRateReader — driving adapter for real-time PV/consumption rates.

Two flavours of "current rate" data, used by PvForecastService for two
different extrapolation strategies of the in-progress 30-min bucket:

1. Bucket-so-far utility meter values (kWh accumulated since :00/:30 reset):
   - sensor.total_pv_generation_bi_hourly
   - sensor.total_consumption_minus_bi_hourly  (minus water heater — matches
     the CONSUMPTION_PER_30MIN baseline used by target_soc)
   Used for "realized prorate" — full bucket = realized × 30 / elapsed_min,
   same logic as the dashboard PV Gen / Cons -Water chart series.

2. 5-minute averaged power sensors (W):
   - sensor.pv_power_avg_5_minutes
   - sensor.house_consumption_avg_5_minutes
   Used for "5-min live rate" — current rate as kW (= kWh/h equivalent).

Returns None when sensors are unavailable so the caller can emit 'unknown'
instead of stale or fabricated data.

Hexagonal pattern: **driving adapter (inbound)** — domain dictates "give me
the current PV/consumption rates", the concrete impl reads HA states.
"""

from __future__ import annotations

import logging
from typing import Final

from homeassistant.core import HomeAssistant

_PV_POWER_5MIN_ENTITY: Final = "sensor.pv_power_avg_5_minutes"
_CONSUMPTION_5MIN_ENTITY: Final = "sensor.house_consumption_avg_5_minutes"
_PV_BUCKET_KWH_ENTITY: Final = "sensor.total_pv_generation_bi_hourly"
_CONSUMPTION_BUCKET_KWH_ENTITY: Final = "sensor.total_consumption_minus_bi_hourly"

_LOGGER = logging.getLogger(__name__)


class LiveRateReader:
    """Reads live PV power / consumption rates + bucket-so-far utility meter values."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def read_pv_power_w(self) -> float | None:
        return self._read_float(_PV_POWER_5MIN_ENTITY)

    def read_consumption_w(self) -> float | None:
        return self._read_float(_CONSUMPTION_5MIN_ENTITY)

    def read_pv_bucket_so_far_kwh(self) -> float | None:
        """KWh accumulated in current 30-min utility meter cycle (resets at :00/:30)."""
        return self._read_float(_PV_BUCKET_KWH_ENTITY)

    def read_consumption_bucket_so_far_kwh(self) -> float | None:
        """KWh accumulated in current 30-min utility meter cycle (minus water heater)."""
        return self._read_float(_CONSUMPTION_BUCKET_KWH_ENTITY)

    def _read_float(self, entity_id: str) -> float | None:
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None
