"""PvForecastSensor — weather-adjusted PV forecast + target SOC sensors."""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any, Final

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import UnitOfEnergy

from ..application.pv_forecast_service import PvForecastService
from ..const import DOMAIN
from ..coordinator import SmartRceDataUpdateCoordinator
from ._helpers import register_state_writer

UNIQUE_ID_PREFIX: Final = DOMAIN

_LOGGER = logging.getLogger(__name__)


class PvForecastSensor(SensorEntity):
    """Sensor for weather-adjusted PV forecast data."""

    _attr_has_entity_name = True

    def __init__(
        self,
        pv_forecast: PvForecastService,
        rce_coordinator: SmartRceDataUpdateCoordinator,
        name: str,
        value_fn: Callable[[PvForecastService], float | int | None],
        attr_fn: Callable[[PvForecastService], dict[str, Any]],
        unit: str | None = None,
    ) -> None:
        self._pv_forecast = pv_forecast
        self._value_fn = value_fn
        self._attr_fn = attr_fn
        self._attr_name = name
        key = name.lower().replace(" ", "_")
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_{key}"
        self._attr_device_info = rce_coordinator.device_info
        if unit:
            self._attr_native_unit_of_measurement = unit
        else:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_state_class = SensorStateClass.MEASUREMENT

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        register_state_writer(self, self._pv_forecast)
        _LOGGER.debug(
            "Setup of PV Forecast sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @property
    def native_value(self) -> float | int | None:
        return self._value_fn(self._pv_forecast)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attr_fn(self._pv_forecast)


def build_pv_forecast_sensors(
    pv_forecast: PvForecastService,
    coordinator: SmartRceDataUpdateCoordinator,
) -> list[PvForecastSensor]:
    """Instantiate all PV forecast + target SOC sensors (kWh + percent variants)."""
    return [
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Weather Adjusted PV At 6",
            lambda pv: pv.forecast.adjusted_at_6.total_kwh
            if pv.forecast.adjusted_at_6
            else None,
            lambda pv: _pv_forecast_attrs(pv.forecast.adjusted_at_6),
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Weather Adjusted PV Live",
            lambda pv: pv.forecast.adjusted_live.total_kwh
            if pv.forecast.adjusted_live
            else None,
            lambda pv: _pv_forecast_attrs(pv.forecast.adjusted_live),
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Weather Adjusted PV Tomorrow At 6",
            lambda pv: pv.forecast.adjusted_tomorrow.total_kwh
            if pv.forecast.adjusted_tomorrow
            else None,
            lambda pv: _pv_forecast_attrs(pv.forecast.adjusted_tomorrow),
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Weather Adjusted PV Tomorrow Live",
            lambda pv: pv.forecast.adjusted_tomorrow_live.total_kwh
            if pv.forecast.adjusted_tomorrow_live
            else None,
            lambda pv: _pv_forecast_attrs(pv.forecast.adjusted_tomorrow_live),
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC At 6",
            lambda pv: pv.forecast.target_soc.value if pv.forecast.target_soc else None,
            lambda pv: _target_soc_trace_attrs(pv.forecast.target_soc),
            unit="%",
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Live",
            lambda pv: pv.forecast.target_soc_live.value
            if pv.forecast.target_soc_live
            else None,
            lambda pv: _target_soc_trace_attrs(pv.forecast.target_soc_live),
            unit="%",
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Tomorrow At 6",
            lambda pv: pv.forecast.target_soc_tomorrow.value
            if pv.forecast.target_soc_tomorrow
            else None,
            lambda pv: _target_soc_trace_attrs(pv.forecast.target_soc_tomorrow),
            unit="%",
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Tomorrow Live",
            lambda pv: pv.forecast.target_soc_tomorrow_live.value
            if pv.forecast.target_soc_tomorrow_live
            else None,
            lambda pv: _target_soc_trace_attrs(pv.forecast.target_soc_tomorrow_live),
            unit="%",
        ),
        # --- Prev-workday instrumentation (Etap A) — today ---
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Prev Day 1",
            lambda pv: pv.forecast.target_soc_prev_days[0].value
            if pv.forecast.target_soc_prev_days[0]
            else None,
            lambda pv: _target_soc_trace_attrs(
                pv.forecast.target_soc_prev_days[0],
                pv.forecast.consumption_profiles[0],
            ),
            unit="%",
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Prev Day 2",
            lambda pv: pv.forecast.target_soc_prev_days[1].value
            if pv.forecast.target_soc_prev_days[1]
            else None,
            lambda pv: _target_soc_trace_attrs(
                pv.forecast.target_soc_prev_days[1],
                pv.forecast.consumption_profiles[1],
            ),
            unit="%",
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Prev Day 3",
            lambda pv: pv.forecast.target_soc_prev_days[2].value
            if pv.forecast.target_soc_prev_days[2]
            else None,
            lambda pv: _target_soc_trace_attrs(
                pv.forecast.target_soc_prev_days[2],
                pv.forecast.consumption_profiles[2],
            ),
            unit="%",
        ),
        # --- Prev-workday instrumentation — tomorrow ---
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Tomorrow Prev Day 1",
            lambda pv: pv.forecast.target_soc_tomorrow_prev_days[0].value
            if pv.forecast.target_soc_tomorrow_prev_days[0]
            else None,
            lambda pv: _target_soc_trace_attrs(
                pv.forecast.target_soc_tomorrow_prev_days[0],
                pv.forecast.consumption_profiles[0],
            ),
            unit="%",
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Tomorrow Prev Day 2",
            lambda pv: pv.forecast.target_soc_tomorrow_prev_days[1].value
            if pv.forecast.target_soc_tomorrow_prev_days[1]
            else None,
            lambda pv: _target_soc_trace_attrs(
                pv.forecast.target_soc_tomorrow_prev_days[1],
                pv.forecast.consumption_profiles[1],
            ),
            unit="%",
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Tomorrow Prev Day 3",
            lambda pv: pv.forecast.target_soc_tomorrow_prev_days[2].value
            if pv.forecast.target_soc_tomorrow_prev_days[2]
            else None,
            lambda pv: _target_soc_trace_attrs(
                pv.forecast.target_soc_tomorrow_prev_days[2],
                pv.forecast.consumption_profiles[2],
            ),
            unit="%",
        ),
        # --- Max safety sensors — max(live, prev_day_1..N) ---
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Max",
            lambda pv: pv.forecast.target_soc_max,
            lambda _pv: {},
            unit="%",
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Tomorrow Max",
            lambda pv: pv.forecast.target_soc_tomorrow_max,
            lambda _pv: {},
            unit="%",
        ),
    ]


def _pv_forecast_attrs(forecast) -> dict[str, Any]:
    if not forecast:
        return {}
    return {
        "forecast": [
            {
                "period_start": p.period_start,
                "pv_estimate_adjusted": p.pv_estimate_adjusted,
            }
            for p in forecast.forecast
        ]
    }


def _target_soc_trace_attrs(result, profile=None) -> dict[str, Any]:
    """Trace for target_soc_* sensors: per-bucket pv/cons/balance/cumulative + is_min.

    If profile is given and has source_date, adds 'profile_date' attribute
    (informs which prev-workday consumption profile was used).
    """
    if not result or not result.buckets:
        return {}
    attrs: dict[str, Any] = {
        "buckets": [
            {
                "period": b.period,
                "pv": b.pv_kwh,
                "cons": b.cons_kwh,
                "balance": b.balance,
                "cumulative": b.cumulative,
                "is_min": b.is_min,
            }
            for b in result.buckets
        ]
    }
    if profile is not None and profile.source_date is not None:
        attrs["profile_date"] = profile.source_date.isoformat()
    return attrs
