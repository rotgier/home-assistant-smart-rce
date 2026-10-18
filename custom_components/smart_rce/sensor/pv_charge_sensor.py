"""PV charge — when the battery got (or will get) full from the sun on the plan day.

State: HH:MM the battery got full today, or is forecast to (attribute
`full_forecast` tells which); None when the PV window ends before. Attribute
`bands`: the stretches the price chart shades — up to 90% and 90 → 100%, solid
for what happened, dashed for the forecast.

Two numeric companions, for HA History: `sensor.ems_pv_charge_90_at` and
`sensor.ems_pv_charge_100_at` — the hour of the day (11.32 = 11:19) the
battery reaches 90% / 100%: the forecast until it gets there, then the hour it
did. A line in History shows how the forecast moved and where it landed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import MATCH_ALL
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..domain.charge_start_shift import to_hhmm
from ..ems_device import ems_device_info

if TYPE_CHECKING:
    from .. import SmartRceConfigEntry
    from ..application.pv_charge_service import PvChargeService


def build_pv_charge_sensors(entry: SmartRceConfigEntry) -> list[SensorEntity]:
    return [
        PvChargeSensor(entry),
        PvChargeHourSensor(entry, "90"),
        PvChargeHourSensor(entry, "100"),
    ]


class PvChargeSensor(SensorEntity):
    """Plan day's PV charge: full at HH:MM, bands for the chart."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:solar-power-variant"
    # The bands are for the live chart; the run itself is persisted by smart_rce.
    # A forecast walked from now moves by a minute as the clock does.
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._service: PvChargeService = entry.runtime_data.pv_charge
        self._attr_unique_id = f"{DOMAIN}_ems_pv_charge"
        self.entity_id = "sensor.ems_pv_charge"
        self._attr_name = "EMS PV Charge"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.async_add_listener(self._on_update))

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        outlook = self._service.outlook
        if outlook is None or outlook.full_at is None:
            return None
        return to_hhmm(outlook.full_at)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        outlook = self._service.outlook
        if outlook is None:
            return None
        return outlook.to_dict(dt_util.get_default_time_zone())


class PvChargeHourSensor(SensorEntity):
    """Hour of the day the battery reaches 90% or 100% — forecast, then fact."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:battery-clock-outline"
    _attr_native_unit_of_measurement = "h"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, entry: SmartRceConfigEntry, level: str) -> None:
        self._service: PvChargeService = entry.runtime_data.pv_charge
        self._level = level
        self._attr_unique_id = f"{DOMAIN}_ems_pv_charge_{level}_at"
        self.entity_id = f"sensor.ems_pv_charge_{level}_at"
        self._attr_name = f"EMS PV Charge {level}% At"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.async_add_listener(self._on_update))

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()

    def _reached(self) -> tuple[int | None, bool]:
        outlook = self._service.outlook
        if outlook is None:
            return None, False
        if self._level == "90":
            return outlook.plateau_at, outlook.plateau_forecast
        return outlook.full_at, outlook.full_forecast

    @property
    def native_value(self) -> float | None:
        minute, _forecast = self._reached()
        return round(minute / 60, 2) if minute is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        outlook = self._service.outlook
        if outlook is None:
            return None
        minute, forecast = self._reached()
        return {
            "day": outlook.day.isoformat(),
            "forecast": forecast,
            "at": self._at(minute, outlook.end_soc),
        }

    @staticmethod
    def _at(minute: int | None, end_soc: float | None) -> str | None:
        """HH:MM, or the level reached by T2 when the charge stops short."""
        if minute is not None:
            return to_hhmm(minute)
        return f"{round(end_soc)}% do T2" if end_soc is not None else None
