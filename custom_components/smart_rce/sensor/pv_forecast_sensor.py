"""PvForecastSensor — weather-adjusted PV forecast + target SOC sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
from typing import Any, Final

from homeassistant.components.sensor import SensorEntityDescription, SensorStateClass
from homeassistant.const import UnitOfEnergy

from ..application.pv_forecast_service import PvForecastService
from ..const import DOMAIN
from ..coordinator import SmartRceDataUpdateCoordinator
from ._state_writer_mixin import StateWriterMixin

UNIQUE_ID_PREFIX: Final = DOMAIN

_LOGGER = logging.getLogger(__name__)


class PvForecastSensor(StateWriterMixin):
    """Sensor for weather-adjusted PV forecast data."""

    _attr_has_entity_name = True
    entity_description: PvForecastSensorDescription

    def __init__(
        self,
        pv_forecast: PvForecastService,
        rce_coordinator: SmartRceDataUpdateCoordinator,
        description: PvForecastSensorDescription,
    ) -> None:
        self._pv_forecast = pv_forecast
        self.entity_description = description
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_{description.key}"
        self._attr_device_info = rce_coordinator.device_info

        # Default kWh + measurement state class; description override (e.g. "%")
        # disables state_class because percentage SOC is not a measurement.
        if description.native_unit_of_measurement:
            self._attr_native_unit_of_measurement = (
                description.native_unit_of_measurement
            )
        else:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_state_class = SensorStateClass.MEASUREMENT

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._register_state_writer(self._pv_forecast)
        _LOGGER.debug(
            "Setup of PV Forecast sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @property
    def native_value(self) -> float | int | None:
        return self.entity_description.value_fn(self._pv_forecast)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.entity_description.attr_fn(self._pv_forecast)


@dataclass(frozen=False, kw_only=True)
class PvForecastSensorDescription(SensorEntityDescription):
    """Description schema for PvForecastSensor — value_fn/attr_fn lambdas."""

    key: str = field(init=False)
    value_fn: Callable[[PvForecastService], float | int | None]
    attr_fn: Callable[[PvForecastService], dict[str, Any]] = lambda _: {}

    def __post_init__(self):
        self.key = self.name.lower().replace(" ", "_")


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


PV_FORECAST_DESCRIPTIONS: tuple[PvForecastSensorDescription, ...] = (
    # --- Weather-adjusted PV forecast (kWh, default unit) ---
    PvForecastSensorDescription(
        name="Weather Adjusted PV At 6",
        value_fn=lambda pv: pv.forecast.adjusted_at_6.total_kwh
        if pv.forecast.adjusted_at_6
        else None,
        attr_fn=lambda pv: _pv_forecast_attrs(pv.forecast.adjusted_at_6),
    ),
    PvForecastSensorDescription(
        name="Weather Adjusted PV Live",
        value_fn=lambda pv: pv.forecast.adjusted_live.total_kwh
        if pv.forecast.adjusted_live
        else None,
        attr_fn=lambda pv: _pv_forecast_attrs(pv.forecast.adjusted_live),
    ),
    PvForecastSensorDescription(
        name="Weather Adjusted PV Tomorrow At 6",
        value_fn=lambda pv: pv.forecast.adjusted_tomorrow.total_kwh
        if pv.forecast.adjusted_tomorrow
        else None,
        attr_fn=lambda pv: _pv_forecast_attrs(pv.forecast.adjusted_tomorrow),
    ),
    PvForecastSensorDescription(
        name="Weather Adjusted PV Tomorrow Live",
        value_fn=lambda pv: pv.forecast.adjusted_tomorrow_live.total_kwh
        if pv.forecast.adjusted_tomorrow_live
        else None,
        attr_fn=lambda pv: _pv_forecast_attrs(pv.forecast.adjusted_tomorrow_live),
    ),
    # --- Extrapolated live variants (per-minute tick) ---
    # state = kWh remaining today (past excluded, current scaled)
    # forecast attribute = full per-period day with current bucket rescaled
    # (chart-friendly — same shape as Adj PV Live so adjusted_pv() helper works)
    PvForecastSensorDescription(
        name="Weather Adjusted PV Live Extrapolated",
        value_fn=lambda pv: pv.forecast.extrapolated_live.remaining_kwh,
        attr_fn=lambda pv: _pv_forecast_attrs(pv.forecast.extrapolated_live.adjusted),
    ),
    PvForecastSensorDescription(
        name="Weather Adjusted PV Live Extrapolated 5min",
        value_fn=lambda pv: pv.forecast.extrapolated_live_5min.remaining_kwh,
        attr_fn=lambda pv: _pv_forecast_attrs(
            pv.forecast.extrapolated_live_5min.adjusted
        ),
    ),
    PvForecastSensorDescription(
        name="Weather Adjusted PV Live Extrapolated Pattern",
        value_fn=lambda pv: pv.forecast.extrapolated_live_pattern.remaining_kwh,
        attr_fn=lambda pv: _pv_forecast_attrs(
            pv.forecast.extrapolated_live_pattern.adjusted
        ),
    ),
    PvForecastSensorDescription(
        name="Weather Adjusted PV Live Extrapolated Proportional",
        value_fn=lambda pv: pv.forecast.extrapolated_live_proportional.remaining_kwh,
        attr_fn=lambda pv: _pv_forecast_attrs(
            pv.forecast.extrapolated_live_proportional.adjusted
        ),
    ),
    PvForecastSensorDescription(
        name="Weather Adjusted PV Live Extrapolated Band",
        value_fn=lambda pv: pv.forecast.extrapolated_live_band.remaining_kwh,
        attr_fn=lambda pv: _pv_forecast_attrs(
            pv.forecast.extrapolated_live_band.adjusted
        ),
    ),
    PvForecastSensorDescription(
        name="Weather Adjusted PV Live Extrapolated Band Recent",
        value_fn=lambda pv: pv.forecast.extrapolated_live_band_recent.remaining_kwh,
        attr_fn=lambda pv: _pv_forecast_attrs(
            pv.forecast.extrapolated_live_band_recent.adjusted
        ),
    ),
    # --- Target SOC (%) ---
    PvForecastSensorDescription(
        name="Target Battery SOC At 6",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc.value
        if pv.forecast.target_soc
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(pv.forecast.target_soc),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Live",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_live.value
        if pv.forecast.target_soc_live
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(pv.forecast.target_soc_live),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Tomorrow At 6",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_tomorrow.value
        if pv.forecast.target_soc_tomorrow
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(pv.forecast.target_soc_tomorrow),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Tomorrow Live",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_tomorrow_live.value
        if pv.forecast.target_soc_tomorrow_live
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.target_soc_tomorrow_live
        ),
    ),
    # --- Target SOC live extrapolated (per-minute tick, in-progress bucket scaled) ---
    PvForecastSensorDescription(
        name="Target Battery SOC Live Extrapolated",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.extrapolated_live.target_soc.value
        if pv.forecast.extrapolated_live.target_soc
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.extrapolated_live.target_soc
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Live Extrapolated 5min",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.extrapolated_live_5min.target_soc.value
        if pv.forecast.extrapolated_live_5min.target_soc
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.extrapolated_live_5min.target_soc
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Live Extrapolated Pattern",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.extrapolated_live_pattern.target_soc.value
        if pv.forecast.extrapolated_live_pattern.target_soc
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.extrapolated_live_pattern.target_soc
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Live Extrapolated Proportional",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.extrapolated_live_proportional.target_soc.value
        if pv.forecast.extrapolated_live_proportional.target_soc
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.extrapolated_live_proportional.target_soc
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Live Extrapolated Band",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.extrapolated_live_band.target_soc.value
        if pv.forecast.extrapolated_live_band.target_soc
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.extrapolated_live_band.target_soc
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Live Extrapolated Band Recent",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.extrapolated_live_band_recent.target_soc.value
        if pv.forecast.extrapolated_live_band_recent.target_soc
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.extrapolated_live_band_recent.target_soc
        ),
    ),
    # --- Prev-workday instrumentation (Etap A) — today ---
    PvForecastSensorDescription(
        name="Target Battery SOC Prev Day 1",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_prev_days[0].value
        if pv.forecast.target_soc_prev_days[0]
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.target_soc_prev_days[0],
            pv.forecast.consumption_profiles[0],
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Prev Day 2",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_prev_days[1].value
        if pv.forecast.target_soc_prev_days[1]
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.target_soc_prev_days[1],
            pv.forecast.consumption_profiles[1],
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Prev Day 3",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_prev_days[2].value
        if pv.forecast.target_soc_prev_days[2]
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.target_soc_prev_days[2],
            pv.forecast.consumption_profiles[2],
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Prev Day 4",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_prev_days[3].value
        if pv.forecast.target_soc_prev_days[3]
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.target_soc_prev_days[3],
            pv.forecast.consumption_profiles[3],
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Prev Day 5",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_prev_days[4].value
        if pv.forecast.target_soc_prev_days[4]
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.target_soc_prev_days[4],
            pv.forecast.consumption_profiles[4],
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Prev Day 6",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_prev_days[5].value
        if pv.forecast.target_soc_prev_days[5]
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.target_soc_prev_days[5],
            pv.forecast.consumption_profiles[5],
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Prev Day 7",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_prev_days[6].value
        if pv.forecast.target_soc_prev_days[6]
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.target_soc_prev_days[6],
            pv.forecast.consumption_profiles[6],
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Prev Day 8",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_prev_days[7].value
        if pv.forecast.target_soc_prev_days[7]
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.target_soc_prev_days[7],
            pv.forecast.consumption_profiles[7],
        ),
    ),
    # --- Prev-workday instrumentation — tomorrow ---
    PvForecastSensorDescription(
        name="Target Battery SOC Tomorrow Prev Day 1",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_tomorrow_prev_days[0].value
        if pv.forecast.target_soc_tomorrow_prev_days[0]
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.target_soc_tomorrow_prev_days[0],
            pv.forecast.consumption_profiles[0],
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Tomorrow Prev Day 2",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_tomorrow_prev_days[1].value
        if pv.forecast.target_soc_tomorrow_prev_days[1]
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.target_soc_tomorrow_prev_days[1],
            pv.forecast.consumption_profiles[1],
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Tomorrow Prev Day 3",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_tomorrow_prev_days[2].value
        if pv.forecast.target_soc_tomorrow_prev_days[2]
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.target_soc_tomorrow_prev_days[2],
            pv.forecast.consumption_profiles[2],
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Tomorrow Prev Day 4",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_tomorrow_prev_days[3].value
        if pv.forecast.target_soc_tomorrow_prev_days[3]
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.target_soc_tomorrow_prev_days[3],
            pv.forecast.consumption_profiles[3],
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Tomorrow Prev Day 5",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_tomorrow_prev_days[4].value
        if pv.forecast.target_soc_tomorrow_prev_days[4]
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.target_soc_tomorrow_prev_days[4],
            pv.forecast.consumption_profiles[4],
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Tomorrow Prev Day 6",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_tomorrow_prev_days[5].value
        if pv.forecast.target_soc_tomorrow_prev_days[5]
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.target_soc_tomorrow_prev_days[5],
            pv.forecast.consumption_profiles[5],
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Tomorrow Prev Day 7",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_tomorrow_prev_days[6].value
        if pv.forecast.target_soc_tomorrow_prev_days[6]
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.target_soc_tomorrow_prev_days[6],
            pv.forecast.consumption_profiles[6],
        ),
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Tomorrow Prev Day 8",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_tomorrow_prev_days[7].value
        if pv.forecast.target_soc_tomorrow_prev_days[7]
        else None,
        attr_fn=lambda pv: _target_soc_trace_attrs(
            pv.forecast.target_soc_tomorrow_prev_days[7],
            pv.forecast.consumption_profiles[7],
        ),
    ),
    # --- Max safety sensors — max(live, prev_day_1..N) ---
    PvForecastSensorDescription(
        name="Target Battery SOC Max",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_max,
    ),
    PvForecastSensorDescription(
        name="Target Battery SOC Tomorrow Max",
        native_unit_of_measurement="%",
        value_fn=lambda pv: pv.forecast.target_soc_tomorrow_max,
    ),
)
