"""Smart RCE number platform — entities for user-tunable numeric knobs.

Currently exposes one entity:
- `number.ems_water_heater_reserved` — manual override for
  `WaterHeaterReservedPolicy.manual_value` (W). Active when the companion
  `select.ems_water_heater_reserved_mode` is set to MANUAL.

Persistence owned by `WaterHeaterReservedRepository`.
"""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SmartRceConfigEntry
from .const import DOMAIN
from .ems_device import ems_device_info

PARALLEL_UPDATES = 1

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartRceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add smart_rce number entities."""
    async_add_entities([EmsWaterHeaterReservedNumber(entry)])


class EmsWaterHeaterReservedNumber(NumberEntity):
    """User-set manual_value for water-heater reserved power.

    The value is consumed by `WaterHeaterReservedPolicy.current_value` only
    when mode=MANUAL (see `select.ems_water_heater_reserved_mode`). When
    mode=AUTO, this entity still reflects the persisted manual_value so a
    later flip to MANUAL has a known starting point.
    """

    _attr_has_entity_name = False
    _attr_name = "EMS Water Heater Reserved"
    _attr_should_poll = False
    _attr_icon = "mdi:water-boiler"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_min_value = 1000
    _attr_native_max_value = 6000
    _attr_native_step = 100
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._entry = entry
        self._service = entry.runtime_data.ems.water_heater_reserved_service
        self._attr_unique_id = f"{DOMAIN}_ems_water_heater_reserved"
        self.entity_id = "number.ems_water_heater_reserved"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> float:
        return float(self._service.manual_value)

    async def async_set_native_value(self, value: float) -> None:
        await self._service.set_manual_value(int(value))
