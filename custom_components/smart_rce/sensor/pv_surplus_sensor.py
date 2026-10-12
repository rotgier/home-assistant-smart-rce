"""PV surplus sensors — forecast (median / flat consumption), realized, table.

All read `PvSurplusService`; the three kWh sensors keep long-term statistics so
the forecast at 08:00 can be set against the realized figure at the end of the
window, day after day.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..domain.charge_curve import pp_below_full
from ..ems_device import ems_device_info

if TYPE_CHECKING:
    from .. import SmartRceConfigEntry
    from ..application.pv_surplus_service import PvSurplusService, SurplusForecast
    from ..domain.pv_surplus import SurplusProfile


class _PvSurplusSensor(SensorEntity):
    """Common wiring: EMS device, push updates from the surplus service."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, entry: SmartRceConfigEntry, slug: str, name: str) -> None:
        self._service: PvSurplusService = entry.runtime_data.pv_surplus
        self._attr_unique_id = f"{DOMAIN}_{slug}"
        self.entity_id = f"sensor.{slug}"
        self._attr_name = name
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.async_add_listener(self._on_update))

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()


class PvSurplusForecastSensor(_PvSurplusSensor):
    """Forecast surplus still to come in [charge start, T2) — LIVE PV.

    State = what is left from now (current hour prorated; before the charge
    starts, the whole window). `window_total_kwh` keeps the whole window.
    """

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, entry: SmartRceConfigEntry, *, flat: bool) -> None:
        super().__init__(
            entry,
            "ems_pv_surplus_forecast_flat" if flat else "ems_pv_surplus_forecast",
            "EMS PV Surplus Forecast Flat" if flat else "EMS PV Surplus Forecast",
        )
        self._flat = flat

    def _profile(self, forecast: SurplusForecast) -> SurplusProfile:
        return forecast.flat if self._flat else forecast.prev

    @property
    def native_value(self) -> float | None:
        forecast = self._service.forecast
        if forecast is None:
            return None
        return forecast.remaining(self._profile(forecast), dt_util.now())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        forecast = self._service.forecast
        if forecast is None:
            return None
        profile = self._profile(forecast)
        left = forecast.remaining(profile, dt_util.now())
        return {
            "day": forecast.day.isoformat(),
            "source": forecast.source,
            "day_type": forecast.day_type.value,
            "consumption": "flat 0.9 kWh/h" if self._flat else "median",
            "start_hour": forecast.start_hour,
            "end_hour": forecast.end_hour,
            "window_total_kwh": forecast.total(profile),
            # Room for sun: how many pp below full the surplus still to come
            # refills (charge curve walked down from 100%).
            "room_pp": round(pp_below_full(left)) if left is not None else None,
            "prev_days": [d.isoformat() for d in forecast.prev_days],
            "hours": [h.to_dict() for h in profile.hourly() if 4 <= h.hour < 21],
        }


class PvSurplusRealizedSensor(_PvSurplusSensor):
    """Measured surplus today over [charge start, T2), accumulating."""

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:solar-power"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        super().__init__(entry, "ems_pv_surplus_realized", "EMS PV Surplus Realized")

    @property
    def native_value(self) -> float | None:
        realized = self._service.realized
        return realized.total if realized else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        realized = self._service.realized
        if realized is None:
            return None
        return {
            "day": realized.day.isoformat(),
            "start_hour": realized.start_hour,
            "end_hour": round(realized.end_minute / 60, 2),
            "hours": [
                h.to_dict() for h in realized.profile.hourly() if 4 <= h.hour < 21
            ],
        }


class PvSurplusMatrixSensor(_PvSurplusSensor):
    """Variant x consumption-day table for the dashboard card."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:table"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        super().__init__(entry, "ems_pv_surplus_matrix", "EMS PV Surplus Matrix")

    @property
    def native_value(self) -> str | None:
        forecast = self._service.forecast
        return forecast.day.isoformat() if forecast else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        forecast = self._service.forecast
        if forecast is None:
            return None
        window = (
            f"{forecast.start_hour}–{forecast.end_hour}"
            if forecast.start_hour is not None
            else "—"
        )
        return {
            "date": forecast.day.isoformat(),
            "kind": "today" if forecast.source == "live" else "tomorrow",
            "window_label": f"okno {window} · {forecast.day_type.value}",
            "matrix": forecast.matrix or None,
        }


def build_pv_surplus_sensors(entry: SmartRceConfigEntry) -> list[SensorEntity]:
    return [
        PvSurplusForecastSensor(entry, flat=False),
        PvSurplusForecastSensor(entry, flat=True),
        PvSurplusRealizedSensor(entry),
        PvSurplusMatrixSensor(entry),
    ]
