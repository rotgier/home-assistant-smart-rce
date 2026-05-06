"""EmsSensor — EMS diagnostic sensors (heater_budget, balanced_baseline)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
import logging
from typing import Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower

from ..application.ems import Ems
from ._state_writer_mixin import StateWriterMixin

EMS_UNIQUE_ID_PREFIX: Final = "ems"

_LOGGER = logging.getLogger(__name__)


class EmsSensor(StateWriterMixin):
    """EMS diagnostic sensor (heater_budget, balanced_baseline)."""

    _attr_has_entity_name = True
    entity_description: EmsSensorDescription

    def __init__(
        self,
        device_info,
        ems: Ems,
        description: EmsSensorDescription,
    ) -> None:
        self._attr_device_info = device_info
        self.ems: Ems = ems
        self.entity_description = description
        self._attr_unique_id = f"{EMS_UNIQUE_ID_PREFIX}_{description.key}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._register_state_writer(self.ems)
        self.async_write_ha_state()
        _LOGGER.debug(
            "Setup of EMS sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @cached_property
    def should_poll(self) -> bool:
        return False

    @property
    def native_value(self) -> str | int | float | None:
        return self.entity_description.value_fn(self.ems)


@dataclass(frozen=False, kw_only=True)
class EmsSensorDescription(SensorEntityDescription):
    """Description schema for EmsSensor — value_fn lambda extracting from Ems."""

    key: str = field(init=False)
    value_fn: Callable[[Ems], str | int | float | None]

    def __post_init__(self):
        self.key = self.name.lower().replace(" ", "_")


EMS_SENSOR_DESCRIPTIONS: tuple[EmsSensorDescription, ...] = (
    EmsSensorDescription(
        name="Heater Budget",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.water_heater.balanced_heater_budget,
        icon="mdi:lightning-bolt",
    ),
    EmsSensorDescription(
        name="Balanced Baseline",
        value_fn=lambda ems: ems.water_heater.balanced_baseline,
        icon="mdi:heating-coil",
    ),
    EmsSensorDescription(
        name="Balanced Upgrade Target",
        value_fn=lambda ems: ems.water_heater.balanced_upgrade_target,
        icon="mdi:arrow-up-bold-circle",
    ),
    EmsSensorDescription(
        name="Balanced Export Bonus W",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.water_heater.balanced_export_bonus_w,
        icon="mdi:transmission-tower-export",
    ),
    EmsSensorDescription(
        name="Grid Export Recommended Xset",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.grid_export.recommended_xset,
        icon="mdi:flash",
    ),
    EmsSensorDescription(
        name="Grid Export Recommended EMS Mode",
        value_fn=lambda ems: ems.grid_export.recommended_ems_mode,
        icon="mdi:cog-outline",
    ),
    EmsSensorDescription(
        name="Grid Export Last Decision Reason",
        value_fn=lambda ems: ems.grid_export.last_decision_reason,
        icon="mdi:information-outline",
    ),
)
