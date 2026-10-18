"""PV charge — when the battery got (or will get) full from the sun on the plan day.

State: HH:MM the battery got full today, or is forecast to (attribute
`full_forecast` tells which); None when the PV window ends before. Attribute
`bands`: the stretches the price chart shades — up to 90% and 90 → 100%, solid
for what happened, dashed for the forecast.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..domain.charge_start_shift import to_hhmm
from ..ems_device import ems_device_info

if TYPE_CHECKING:
    from .. import SmartRceConfigEntry
    from ..application.pv_charge_service import PvChargeService


def build_pv_charge_sensors(entry: SmartRceConfigEntry) -> list[SensorEntity]:
    return [PvChargeSensor(entry)]


class PvChargeSensor(SensorEntity):
    """Plan day's PV charge: full at HH:MM, bands for the chart."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:solar-power-variant"

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
