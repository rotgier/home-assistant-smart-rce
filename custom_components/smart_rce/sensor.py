"""Smart RCE Sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Any, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import UnitOfEnergy, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SmartRceConfigEntry
from .const import DOMAIN
from .coordinator import SmartRceDataUpdateCoordinator
from .domain.ems import Ems

CURRENCY_PLN: Final = "zł"
UNIQUE_ID_PREFIX = DOMAIN

PARALLEL_UPDATES = 1


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=False, kw_only=True)
class SmartRceSensorDescription(SensorEntityDescription):
    key: str = field(init=False)
    value_fn: Callable[[Ems], str | int | float | datetime | None]
    attr_fn: Callable[[dict[str, Any]], dict[str, Any]] = lambda _: {}

    def __post_init__(self):
        self.key = self.name.lower().replace(" ", "_")


SENSOR_DESCRIPTIONS: tuple[SmartRceSensorDescription, ...] = (
    SmartRceSensorDescription(
        name="Current Price",
        native_unit_of_measurement=f"{CURRENCY_PLN}/{UnitOfEnergy.MEGA_WATT_HOUR}",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda ems: ems.current_price,
    ),
    ####
    #### TODAY
    ####
    SmartRceSensorDescription(
        name="Start Charge Hour Today",
        native_unit_of_measurement=UnitOfTime.HOURS,
        value_fn=lambda ems: ems.today.start_charge_hour,
    ),
    SmartRceSensorDescription(
        name="Start Charge Hour Today Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.today.start_charge_hour_datetime,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Today",
        native_unit_of_measurement=UnitOfTime.HOURS,
        value_fn=lambda ems: ems.today.end_charge_hour,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Today Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.today.end_charge_hour_datetime,
    ),
    ####
    #### TOMORROW
    ####
    SmartRceSensorDescription(
        name="Start Charge Hour Tomorrow",
        native_unit_of_measurement=UnitOfTime.HOURS,
        value_fn=lambda ems: ems.tomorrow.start_charge_hour,
    ),
    SmartRceSensorDescription(
        name="Start Charge Hour Tomorrow Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.tomorrow.start_charge_hour_datetime,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Tomorrow",
        native_unit_of_measurement=UnitOfTime.HOURS,
        value_fn=lambda ems: ems.tomorrow.end_charge_hour,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Tomorrow Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.tomorrow.end_charge_hour_datetime,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartRceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add Smart RCE sensors."""
    coordinator = entry.runtime_data.rce_coordinator
    ems = entry.runtime_data.ems

    sensors: list[SmartRceSensorDescription] = [
        SmartRceSensor(coordinator, ems, description)
        for description in SENSOR_DESCRIPTIONS
    ]

    async_add_entities(sensors)


class SmartRceSensor(CoordinatorEntity[SmartRceDataUpdateCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: SmartRceSensorDescription

    def __init__(
        self,
        coordinator: SmartRceDataUpdateCoordinator,
        ems: Ems,
        description: SmartRceSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.ems: Ems = ems
        self.entity_description = description
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_{description.key}"
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def listener() -> None:
            self.async_write_ha_state()

        remove_listener = self.ems.async_add_listener(listener)
        setattr(remove_listener, "_hass_callback", True)
        self.async_on_remove(remove_listener)

        self._handle_coordinator_update()
        _LOGGER.debug(
            "Setup of RCE Smart sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @property
    def native_value(self) -> str | int | float | datetime | None:
        return self.entity_description.value_fn(self.ems)
