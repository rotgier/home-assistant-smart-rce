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
   - sensor.house_consumption_minus_water_avg_5_minutes
   Used for "5-min live rate" — current rate as kW (= kWh/h equivalent).
   Consumption sensor subtracts water heaters because heating water from
   PV surplus indicates energy abundance — counting it as "real" load
   would inflate target SOC% during sunny morning bursts.

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
_CONSUMPTION_5MIN_ENTITY: Final = "sensor.house_consumption_minus_water_avg_5_minutes"
_PV_BUCKET_KWH_ENTITY: Final = "sensor.total_pv_generation_bi_hourly"
_CONSUMPTION_BUCKET_KWH_ENTITY: Final = "sensor.total_consumption_minus_bi_hourly"
_START_CHARGE_HOUR_OVERRIDE_ENTITY: Final = (
    "time.ems_battery_charge_start_hour_override"
)
# Phase C: derivative-aware projection inputs. Both built by the
# `pv_stability` HA YAML package — derivative is the 2-min HA derivative
# of `pv_power_avg_2_minutes` (W/min), stability is the threshold sensor
# layered on top of a rolling p95 of |second derivative|.
_PV_DERIVATIVE_ENTITY: Final = "sensor.pv_power_derivative_avg_2min"
_PV_STABILITY_BINARY_ENTITY: Final = "binary_sensor.pv_derivative_is_stable"

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

    def read_pv_derivative_w_per_min(self) -> float | None:
        """PV power first derivative (W/min) — input for ramp projection.

        Source: `sensor.pv_power_derivative_avg_2min` built by HA package
        `pv_stability` as a 2-min derivative on `pv_power_avg_2_minutes`.
        """
        return self._read_float(_PV_DERIVATIVE_ENTITY)

    def read_pv_stability_stable(self) -> bool | None:
        """Whether the PV derivative is currently flagged stable.

        Source: `binary_sensor.pv_derivative_is_stable` (HA threshold +
        hysteresis on a rolling p95 of |second derivative|). Phase C
        gates ramp projection on this signal — `True` means the
        first-derivative motion is steady enough to trust as a slope.
        """
        state = self._hass.states.get(_PV_STABILITY_BINARY_ENTITY)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        return state.state == "on"

    def read_start_charge_hour_today_override(self) -> int | None:
        """Hour (0..23) when pre-charge ends / post-charge begins.

        Etap B'-2: read from smart_rce-owned `time.ems_battery_charge_start_hour_override`
        (replaces legacy `input_datetime.rce_start_charge_hour_today_override`).
        Same source as DodPolicy uses for WORKDAY_PRE/POST_CHARGE phase split,
        sourced from `BatteryChargePolicy.start_charge_hour_override` via the
        HA time entity.

        Parses HH:MM:SS state, returns the hour component. Returns None when
        entity unavailable so caller can fall back to no-gate behavior.
        """
        state = self._hass.states.get(_START_CHARGE_HOUR_OVERRIDE_ENTITY)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return int(state.state.split(":")[0])
        except (ValueError, AttributeError, IndexError):
            return None

    def _read_float(self, entity_id: str) -> float | None:
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None
