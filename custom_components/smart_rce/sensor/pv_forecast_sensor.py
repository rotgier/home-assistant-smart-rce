"""PvForecastSensor — weather-adjusted PV forecast + target SOC sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
from typing import Any, Final

from homeassistant.components.sensor import SensorEntityDescription, SensorStateClass
from homeassistant.const import UnitOfEnergy
from homeassistant.util import dt as dt_util

from ..application.energy_balance_service import EnergyBalanceService
from ..const import DOMAIN
from ..coordinator import SmartRceDataUpdateCoordinator
from ..domain.bucket import Bucket
from ..domain.pv_forecast import PvForecast, PvForecasts
from ._state_writer_mixin import StateWriterMixin

UNIQUE_ID_PREFIX: Final = DOMAIN

_LOGGER = logging.getLogger(__name__)


class PvForecastSensor(StateWriterMixin):
    """Sensor for weather-adjusted PV forecast data."""

    _attr_has_entity_name = True
    entity_description: PvForecastSensorDescription

    def __init__(
        self,
        pv_forecast: EnergyBalanceService,
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
    value_fn: Callable[[EnergyBalanceService], float | int | None]
    attr_fn: Callable[[EnergyBalanceService], dict[str, Any]] = lambda _: {}

    def __post_init__(self):
        self.key = self.name.lower().replace(" ", "_")


def _make_pv_desc(v: PvForecast) -> PvForecastSensorDescription:
    """Build 'PV Forecast <label>' kWh sensor for one PV forecast variant.

    Dispatch by `v.is_extrap`:
    - Extrap variants → `remaining_kwh` (forward-only, current bucket scaled).
      Chart-friendly forecast attr — same shape as Adj PV Live.
    - Non-extrap (AT_6 / LIVE today+tomorrow) → `total_kwh` (full-day sum).

    Default arg `_v=v` captures the loop variable by value (closure gotcha:
    without it all lambdas would close over the same final `v`).
    """
    if v.is_extrap:
        return PvForecastSensorDescription(
            name=f"PV Forecast {v.pretty_label}",
            value_fn=lambda pv, _v=v: pv.forecasts.remaining_kwh(_v),
            attr_fn=lambda pv, _v=v: _pv_forecast_attrs(pv.forecasts.get(_v)),
        )
    return PvForecastSensorDescription(
        name=f"PV Forecast {v.pretty_label}",
        value_fn=lambda pv, _v=v: (
            pv.forecasts.get(_v).total_kwh if pv.forecasts.get(_v) else None
        ),
        attr_fn=lambda pv, _v=v: _pv_forecast_attrs(pv.forecasts.get(_v)),
    )


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


def _make_target_soc_desc(v: PvForecast) -> PvForecastSensorDescription:
    """Build 'Target Battery SOC <label>' (%) sensor for one PV forecast variant.

    Reads `pv.target_socs.target_socs[v].flat.value` as main reading.
    Attributes (`_target_soc_attrs`): prev_day_1..8 (per prev-workday
    cons profile) + max + per-bucket trace. Profile bundle is
    `today_profiles` for today variants, `tomorrow_profiles` otherwise.

    Default arg captures (`_v`, `_pa`) avoid closure-over-loop-variable.
    """
    profiles_attr = "today_profiles" if v.is_today else "tomorrow_profiles"
    return PvForecastSensorDescription(
        name=f"Target SOC {v.pretty_label}",
        native_unit_of_measurement="%",
        value_fn=lambda pv, _v=v: (
            pv.target_socs.target_socs[_v].flat.value
            if pv.target_socs.target_socs[_v].flat
            else None
        ),
        attr_fn=lambda pv, _v=v, _pa=profiles_attr: _target_soc_attrs(
            pv.target_socs.target_socs[_v],
            getattr(pv.target_socs.consumption_profiles, _pa),
        ),
    )


def _target_soc_attrs(entity, profiles) -> dict[str, Any]:
    """Build per-variant TargetSoc sensor attrs: flat trace + 8 prev_days + max + source_dates.

    `entity`: TargetSoc per-variant.
    `profiles`: list of ConsumptionProfile | None (today_profiles or tomorrow_profiles).
    """
    attrs: dict[str, Any] = {}
    for i in range(8):
        if i < len(entity.prev_days):
            r = entity.prev_days[i]
            attrs[f"prev_day_{i + 1}"] = r.value if r is not None else None
        else:
            attrs[f"prev_day_{i + 1}"] = None
    attrs["prev_day_source_dates"] = [
        (
            p.source_date.isoformat()
            if p is not None and p.source_date is not None
            else None
        )
        for p in profiles
    ]
    attrs["max"] = entity.max
    buckets = _buckets_to_dict(entity.flat)
    if buckets:
        attrs["buckets"] = buckets
    return attrs


def _buckets_to_dict(result) -> list[dict[str, Any]]:
    """Serialize result.buckets as list of dicts for sensor trace attrs."""
    if not result or not result.buckets:
        return []
    return [
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


def _bucket_end_constant_kwh(forecasts: PvForecasts) -> float | None:
    """Projected in-progress bucket kWh assuming constant `signals.pv_power_w`."""
    signals = forecasts.signals
    if signals.pv_power_w is None or signals.bucket_so_far_kwh is None:
        return None
    return Bucket.full_bucket_kwh(
        dt_util.now(), signals.pv_power_w, signals.bucket_so_far_kwh
    )


def _bucket_end_derivative_kwh(forecasts: PvForecasts) -> float | None:
    """Projected in-progress bucket kWh using ramp when derivative is stable."""
    signals = forecasts.signals
    if signals.pv_power_w is None or signals.bucket_so_far_kwh is None:
        return None
    return Bucket.full_bucket_kwh(
        dt_util.now(),
        signals.pv_power_w,
        signals.bucket_so_far_kwh,
        derivative_w_per_min=_effective_derivative(forecasts),
    )


def _effective_derivative(forecasts: PvForecasts) -> float:
    """Return the derivative to feed the ramp formula, gated on stability.

    Returns `signals.derivative_w_per_min` when the stability binary is
    True AND the value is available; 0.0 otherwise. A 0.0 derivative
    collapses the ramp integral back to the constant-power formula —
    so callers can pass this unconditionally to `Bucket.full_bucket_kwh`.
    """
    signals = forecasts.signals
    if signals.stability_stable and signals.derivative_w_per_min is not None:
        return signals.derivative_w_per_min
    return 0.0


def _bucket_end_derivative_delta_kwh(forecasts: PvForecasts) -> float | None:
    """Derivative-aware minus constant projection — zero when ramp inactive."""
    deriv_kwh = _bucket_end_derivative_kwh(forecasts)
    const_kwh = _bucket_end_constant_kwh(forecasts)
    if deriv_kwh is None or const_kwh is None:
        return None
    return deriv_kwh - const_kwh


PV_FORECAST_DESCRIPTIONS: tuple[PvForecastSensorDescription, ...] = (
    # Per-variant pair (PV kWh + Target SOC %) — generated from PvForecast enum.
    # Adding a new variant: just add enum member with strategy.pretty_label set.
    *(
        desc
        for v in PvForecast
        for desc in (_make_pv_desc(v), _make_target_soc_desc(v))
    ),
    # --- In-progress bucket projection observability (Phase C.1) ---
    # Compare constant-power vs derivative-aware ramp for the bucket the
    # chart in-progress dot lives in. Delta is non-zero only when the
    # stability binary flags the derivative trustworthy — observation
    # period before deciding whether to switch the chart/target_soc
    # patch to use ramp (Phase C.2).
    PvForecastSensorDescription(
        name="PV Bucket End Constant",
        value_fn=lambda pv: _bucket_end_constant_kwh(pv.forecasts),
    ),
    PvForecastSensorDescription(
        name="PV Bucket End Derivative",
        value_fn=lambda pv: _bucket_end_derivative_kwh(pv.forecasts),
    ),
    PvForecastSensorDescription(
        name="PV Bucket End Derivative Delta",
        value_fn=lambda pv: _bucket_end_derivative_delta_kwh(pv.forecasts),
    ),
    # NOTE: Prev-workday + max sensors retired in Iter 2b. Consumers should
    # read attributes on the corresponding TargetSoc sensors instead:
    # - prev_day_1..8 → state_attr('sensor.rce_target_battery_soc_live', 'prev_day_N')
    # - tomorrow_prev_day_1..8 → state_attr('sensor.rce_target_battery_soc_tomorrow_live', 'prev_day_N')
    # - max → state_attr('sensor.rce_target_battery_soc_live', 'max')
    # - tomorrow_max → state_attr('sensor.rce_target_battery_soc_tomorrow_live', 'max')
    # source_date per prev-workday slot → 'prev_day_source_dates' attribute (list of 8 ISO dates)
)
