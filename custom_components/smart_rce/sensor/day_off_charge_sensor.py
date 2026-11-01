"""Day-off charge decision — store the PV or export it at once (06:41 / 07:41).

State: the chosen variant (`none`, `s0`, `from_<hh>`); attributes: every
variant's value and what the run rested on (`DayOffReport`). Unknown until the
first run of a day off since the start of HA.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback

from ..const import DOMAIN
from ..ems_device import ems_device_info

if TYPE_CHECKING:
    from .. import SmartRceConfigEntry
    from ..application.day_off_charge_service import DayOffChargeService


class DayOffChargeSensor(SensorEntity):
    """The latest day-off decision and its variants."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:solar-power-variant-outline"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._service: DayOffChargeService = entry.runtime_data.day_off_charge
        self._attr_unique_id = f"{DOMAIN}_ems_day_off_charge"
        self.entity_id = "sensor.ems_day_off_charge"
        self._attr_name = "EMS Day Off Charge"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.async_add_listener(self._on_update))

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        report = self._service.last
        return report.decision.chosen if report is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        report = self._service.last
        return report.to_dict() if report is not None else None
