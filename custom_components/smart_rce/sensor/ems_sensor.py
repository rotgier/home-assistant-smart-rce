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
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

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
        entry_id: str,
        ems: Ems,
        description: EmsSensorDescription,
    ) -> None:
        self._attr_device_info = DeviceInfo(
            name="EMS",
            identifiers={(EMS_UNIQUE_ID_PREFIX, entry_id)},
            entry_type=DeviceEntryType.SERVICE,
        )
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
    EmsSensorDescription(
        name="Target DoD",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.dod_policy.target_dod,
        icon="mdi:battery-charging-50",
    ),
    EmsSensorDescription(
        name="DoD Policy Phase",
        value_fn=lambda ems: ems.dod_policy.current_phase.value,
        icon="mdi:state-machine",
    ),
    EmsSensorDescription(
        name="Battery Charge Current",
        # Cached Modbus readback of `battery_charge_current` register (A).
        # smart_rce manages this since Goodwe HA integration doesn't expose
        # an entity for the register — only goodwe.set_parameter /
        # goodwe.get_parameter services. Updated by BatteryChargeCurrentActuator.
        native_unit_of_measurement="A",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.battery_charge_service.modbus_current_value,
        icon="mdi:current-dc",
    ),
    EmsSensorDescription(
        name="Battery Schedule Currently Engaging",
        # Slot kind name (e.g. "DISCHARGE_EVENING") or "IDLE" when no slot
        # active. Diagnostic — UI shows which schedule slot is driving the
        # inverter right now. Updates on engage/disengage events emitted by
        # BatterySchedule.compute_operation (per-tick fan-out via service
        # listeners). Etap 2B observability.
        value_fn=lambda ems: ems.battery_schedule_service.currently_engaging,
        icon="mdi:battery-clock",
    ),
    EmsSensorDescription(
        name="One-Shot Active",
        # One-shot operation summary: "IDLE", "DISCHARGE → 15% until 17:30",
        # or "CHARGE → 80% until 06:00". Etap 2F — surfaces ad-hoc engagement
        # state for dashboard cards. Aggregate flips on
        # start_oneshot/cancel_oneshot or auto-clear in compute_operation
        # (target_reached/expired).
        value_fn=lambda ems: _format_oneshot(ems.battery_schedule_service.oneshot),
        icon="mdi:flash-outline",
    ),
)


def _format_oneshot(op) -> str:
    if op is None:
        return "IDLE"
    return (
        f"{op.direction.name} → {op.target_soc:.0f}% "
        f"until {op.end_at.strftime('%H:%M')}"
    )
