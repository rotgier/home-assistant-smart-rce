"""EmsSensor — EMS diagnostic sensors (heater_budget, heater_baseline, etc.)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
import logging
from typing import Any, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from ..application.ems import Ems
from ..domain.battery_charge_policy import ChargeStartSource
from ..domain.battery_schedule import OneShotOperation, T1SocReserve
from ._state_writer_mixin import StateWriterMixin

EMS_UNIQUE_ID_PREFIX: Final = "ems"

_LOGGER = logging.getLogger(__name__)


class EmsSensor(StateWriterMixin):
    """EMS diagnostic sensor (heater_budget, heater_baseline, etc.)."""

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

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self.entity_description.attr_fn(self.ems)


@dataclass(frozen=True, kw_only=True)
class EmsSensorDescription(SensorEntityDescription):
    """Description schema for EmsSensor — value_fn lambda extracting from Ems."""

    key: str = field(init=False)
    value_fn: Callable[[Ems], str | int | float | None]
    attr_fn: Callable[[Ems], dict[str, Any] | None] = lambda _: None

    def __post_init__(self) -> None:
        assert isinstance(self.name, str)
        object.__setattr__(self, "key", self.name.lower().replace(" ", "_"))


EMS_SENSOR_DESCRIPTIONS: tuple[EmsSensorDescription, ...] = (
    EmsSensorDescription(
        name="Heater Budget",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.water_heater.heater_budget,
        icon="mdi:lightning-bolt",
    ),
    EmsSensorDescription(
        name="Heater Baseline",
        value_fn=lambda ems: ems.water_heater.heater_baseline,
        icon="mdi:heating-coil",
    ),
    EmsSensorDescription(
        name="Heater Upgrade Target",
        value_fn=lambda ems: ems.water_heater.heater_upgrade_target,
        icon="mdi:arrow-up-bold-circle",
    ),
    EmsSensorDescription(
        name="Heater Export Bonus",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.water_heater.heater_export_bonus,
        icon="mdi:transmission-tower-export",
    ),
    EmsSensorDescription(
        name="Battery Charge Start Source",
        device_class=SensorDeviceClass.ENUM,
        options=[source.value for source in ChargeStartSource],
        value_fn=lambda ems: ems.battery_charge_service.start_charge_source.value,
        attr_fn=lambda ems: ems.battery_charge_service.start_charge_details(),
        icon="mdi:source-branch",
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
        # Slot kind name (e.g. "DISCHARGE_EVENING_EARLY") or "IDLE" when no slot
        # active. Diagnostic — UI shows which schedule slot is driving the
        # inverter right now. Updates on engage/disengage events emitted by
        # BatterySchedule.compute_operation (per-tick fan-out via service
        # listeners). Etap 2B observability.
        value_fn=lambda ems: ems.battery_schedule_service.currently_engaging,
        icon="mdi:battery-clock",
    ),
    EmsSensorDescription(
        name="Battery Schedule Sessions",
        # Number of slot sessions today. Attributes: `sessions` — each with
        # start/end, the SoC it stopped at and the target then in force (the
        # price chart shades them); `upcoming` — runs of the auto slots still
        # ahead today with the expected DELAYED_TO_END start and target.
        value_fn=lambda ems: len(ems.battery_schedule_service.sessions_today),
        attr_fn=lambda ems: {
            "sessions": [
                s.to_dict() for s in ems.battery_schedule_service.sessions_today
            ],
            "upcoming": [
                r.to_dict() for r in ems.battery_schedule_service.upcoming_runs
            ],
        },
        icon="mdi:battery-sync",
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
    EmsSensorDescription(
        name="T1 SoC Reserve",
        # SoC to hold for the rest of the T1 window (07-13, workdays), LIVE
        # variant. Before 07:00 the whole window ahead — the target of the
        # CHARGE_MORNING_AUTO slot; 07-13 what is left (falls); 13-16 today's
        # window replayed in full; from 16:00 tomorrow's window (TOMORROW_LIVE).
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: _reserve_field(
            ems.battery_schedule_service.t1_reserve, "value"
        ),
        attr_fn=lambda ems: _reserve_attrs(ems.battery_schedule_service.t1_reserve),
        icon="mdi:battery-clock-outline",
    ),
    EmsSensorDescription(
        name="T1 SoC Reserve Flat",
        # Component of the reserve: default consumption (0.45 kWh / 30 min).
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: _reserve_field(
            ems.battery_schedule_service.t1_reserve, "flat"
        ),
        icon="mdi:battery-clock-outline",
    ),
    EmsSensorDescription(
        name="T1 SoC Reserve Prev",
        # Component of the reserve: prev-workday profiles without the worst one.
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: _reserve_field(
            ems.battery_schedule_service.t1_reserve, "prev_without_worst"
        ),
        icon="mdi:battery-clock-outline",
    ),
    EmsSensorDescription(
        name="T1 SoC Reserve P60",
        # Component that drives the reserve: per-bucket 60th percentile of the
        # earlier workdays' consumption (falls back to flat without history).
        # Replaced "T1 SoC Reserve Median" on 15.10.2026 — a new entity, the
        # median history is a different quantity.
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: _reserve_field(
            ems.battery_schedule_service.t1_reserve, "p60"
        ),
        icon="mdi:battery-clock-outline",
    ),
    EmsSensorDescription(
        name="T1 SoC Reserve Dip",
        # Signed dip of the reading behind the reserve (p60, else flat):
        # positive = deficit at the tightest point, negative = headroom (the
        # buffer fades out by ~-0.97 kWh). No energy device class — it goes negative.
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: _reserve_field(
            ems.battery_schedule_service.t1_reserve, "dip_kwh"
        ),
        icon="mdi:chart-bell-curve",
    ),
    EmsSensorDescription(
        name="Morning Discharge Plan",
        # Target of the automatic morning discharge (DISCHARGE_MORNING_AUTO)
        # for today's dearest hour 07-10; attributes say which hour, reserve,
        # the lowest SoC the sun fills and which rule branch set it.
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: (
            plan.target
            if (plan := ems.battery_schedule_service.morning_discharge) is not None
            else None
        ),
        attr_fn=lambda ems: (
            plan.to_dict()
            if (plan := ems.battery_schedule_service.morning_discharge) is not None
            else None
        ),
        icon="mdi:battery-arrow-down-outline",
    ),
    EmsSensorDescription(
        name="T1 SoC Reserve Extrap",
        # Highest reserve over today's LIVE + 4 EXTRAP variants, until 16:00
        # (EXTRAP has no tomorrow). Observation only — nothing acts on it yet.
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: _reserve_field(
            ems.battery_schedule_service.t1_reserve_extrap, "value"
        ),
        attr_fn=lambda ems: _reserve_attrs(
            ems.battery_schedule_service.t1_reserve_extrap
        ),
        icon="mdi:battery-clock-outline",
    ),
)


def _reserve_field(reserve: T1SocReserve | None, field_name: str) -> Any:
    return getattr(reserve, field_name) if reserve is not None else None


def _reserve_attrs(reserve: T1SocReserve | None) -> dict[str, Any] | None:
    return reserve.to_dict() if reserve is not None else None


def _format_oneshot(op: OneShotOperation | None) -> str:
    if op is None:
        return "IDLE"
    return (
        f"{op.direction.name} → {op.target_soc:.0f}% "
        f"until {op.end_at.strftime('%H:%M')}"
    )
