"""EMS binary sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SmartRceConfigEntry
from .domain.ems import Ems

UNIQUE_ID_PREFIX = "ems"

PARALLEL_UPDATES = 1


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=False, kw_only=True)
class EmsBinarySensorDescription(BinarySensorEntityDescription):
    key: str = field(init=False)
    value_fn: Callable[[Ems], bool]
    attr_fn: Callable[[dict[str, Any]], dict[str, Any]] = lambda _: {}

    def __post_init__(self):
        self.key = self.name.lower().replace(" ", "_")


SENSOR_DESCRIPTIONS: tuple[EmsBinarySensorDescription, ...] = (
    EmsBinarySensorDescription(
        name="Water Heater Turn On",
        value_fn=lambda ems: ems.water_heater.should_turn_on,
        icon="mdi:heating-coil",
    ),
    EmsBinarySensorDescription(
        name="Water Heater Turn Off",
        value_fn=lambda ems: ems.water_heater.should_turn_off,
        icon="mdi:heating-coil",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartRceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # device_info = entry.runtime_data.rce_coordinator.device_info
    device_info = DeviceInfo(
        name="EMS",
        identifiers={("ems", entry.entry_id)},
        entry_type=DeviceEntryType.SERVICE,
    )
    ems = entry.runtime_data.ems

    sensors: list[EmsBinarySensorDescription] = [
        EmsBinarySensor(device_info, ems, description)
        for description in SENSOR_DESCRIPTIONS
    ]

    async_add_entities(sensors)


class EmsBinarySensor(BinarySensorEntity):
    _attr_has_entity_name = True
    entity_description: EmsBinarySensorDescription

    def __init__(
        self,
        device_info: DeviceInfo,
        ems: Ems,
        description: EmsBinarySensorDescription,
    ) -> None:
        self._attr_device_info = device_info
        self.ems: Ems = ems
        self.entity_description = description
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_{description.key}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def listener() -> None:
            self.async_write_ha_state()

        remove_listener = self.ems.async_add_listener(listener)
        setattr(remove_listener, "_hass_callback", True)
        self.async_on_remove(remove_listener)

        self.async_write_ha_state()
        _LOGGER.debug(
            "Setup of EMS binary sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @cached_property
    def should_poll(self) -> bool:
        return False

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.ems)
