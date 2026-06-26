"""Smart RCE number platform — entities for user-tunable numeric knobs.

Exposes (among others):
- Charge-window algorithm params (Battery charge): extend threshold, absolute
  cheap price, base-window shift — `BatteryChargePolicy`, written via Ems
  (recompute + start force-sync).
- Water-heater reserved power + bonus gates — `WaterHeaterReservedPolicy`.
- Battery-schedule per-slot + one-shot target SoC — `BatterySchedule`.

Persistence owned by the respective repositories.
"""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SmartRceConfigEntry
from .const import DOMAIN
from .domain.battery_schedule import (
    Direction,
    Scope,
    SetOneShotTargetSocCommand,
    SetSlotTargetSocCommand,
    SlotKind,
)
from .ems_device import ems_device_info
from .garden.number_entities import build_numbers

PARALLEL_UPDATES = 1

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartRceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add smart_rce number entities."""
    scopes: tuple[Scope, ...] = ("today", "tomorrow")
    async_add_entities(
        [
            EmsBatteryChargeExtendThresholdNumber(entry),
            EmsBatteryChargeAbsoluteCheapPriceNumber(entry),
            EmsBatteryChargeBaseWindowShiftNumber(entry),
            EmsWaterHeaterReservedNumber(entry),
            EmsWaterHeaterBonusGateOnNumber(entry),
            EmsWaterHeaterBonusGateOffNumber(entry),
            *[
                BatteryScheduleSlotTargetSocNumber(entry, scope=scope, kind=kind)
                for scope in scopes
                for kind in SlotKind
            ],
            OneShotTargetSocNumber(entry, direction=Direction.DISCHARGE),
            OneShotTargetSocNumber(entry, direction=Direction.CHARGE),
            *build_numbers(entry),
        ]
    )


class EmsBatteryChargeExtendThresholdNumber(NumberEntity):
    """Threshold (zł/MWh) to extend the charge window earlier.

    Take an earlier+longer window when the earlier hour is at most this many
    zł/MWh above the base-window max price. Higher = more eager to grab earlier
    hours (safer margin if PV forecast under-delivers). Bridge to
    `BatteryChargePolicy.charge_extend_threshold` via Ems (recompute +
    start force-sync on change — see `Ems._apply_charge_param_change`).
    """

    _attr_has_entity_name = False
    _attr_name = "EMS Battery Charge Extend Threshold"
    _attr_should_poll = False
    _attr_icon = "mdi:cash-clock"
    _attr_native_unit_of_measurement = "PLN/MWh"
    _attr_native_min_value = 0
    _attr_native_max_value = 300
    _attr_native_step = 5
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._entry = entry
        self._ems = entry.runtime_data.ems
        self._service = self._ems.battery_charge_service
        self._attr_unique_id = f"{DOMAIN}_ems_battery_charge_extend_threshold"
        self.entity_id = "number.ems_battery_charge_extend_threshold"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> float:
        return self._service.charge_extend_threshold

    async def async_set_native_value(self, value: float) -> None:
        await self._ems.set_charge_extend_threshold(value)


class EmsBatteryChargeAbsoluteCheapPriceNumber(NumberEntity):
    """Absolute-cheap price (zł/MWh) for extending the charge window earlier.

    An earlier hour cheaper than this is always worth grabbing, regardless of
    the relative extend threshold. Bridge to
    `BatteryChargePolicy.charge_absolute_cheap_price` via Ems.
    """

    _attr_has_entity_name = False
    _attr_name = "EMS Battery Charge Absolute Cheap Price"
    _attr_should_poll = False
    _attr_icon = "mdi:cash-check"
    _attr_native_unit_of_measurement = "PLN/MWh"
    _attr_native_min_value = 0
    _attr_native_max_value = 500
    _attr_native_step = 5
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._entry = entry
        self._ems = entry.runtime_data.ems
        self._service = self._ems.battery_charge_service
        self._attr_unique_id = f"{DOMAIN}_ems_battery_charge_absolute_cheap_price"
        self.entity_id = "number.ems_battery_charge_absolute_cheap_price"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> float:
        return self._service.charge_absolute_cheap_price

    async def async_set_native_value(self, value: float) -> None:
        await self._ems.set_charge_absolute_cheap_price(value)


class EmsBatteryChargeBaseWindowShiftNumber(NumberEntity):
    """Base-window start shift (minutes) — earlier start when window == base.

    When the chosen window stays at the base length, the battery starts this
    many minutes earlier (margin if PV forecast under-delivers). 0 = no shift.
    Bridge to `BatteryChargePolicy.charge_base_window_shift_minutes` via Ems.
    """

    _attr_has_entity_name = False
    _attr_name = "EMS Battery Charge Base Window Shift"
    _attr_should_poll = False
    _attr_icon = "mdi:clock-start"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = 0
    _attr_native_max_value = 120
    _attr_native_step = 5
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._entry = entry
        self._ems = entry.runtime_data.ems
        self._service = self._ems.battery_charge_service
        self._attr_unique_id = f"{DOMAIN}_ems_battery_charge_base_window_shift"
        self.entity_id = "number.ems_battery_charge_base_window_shift"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> float:
        return float(self._service.charge_base_window_shift_minutes)

    async def async_set_native_value(self, value: float) -> None:
        await self._ems.set_charge_base_window_shift_minutes(int(value))


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


class EmsWaterHeaterBonusGateOnNumber(NumberEntity):
    """Bonus threshold (W) to open the gate in `prefer_battery_first` mode.

    Heaters fire only when `export_bonus ≥ this threshold`. See
    `WaterHeaterManager._bonus_gate_open`.
    """

    _attr_has_entity_name = False
    _attr_name = "EMS Water Heater Bonus Gate ON"
    _attr_should_poll = False
    _attr_icon = "mdi:fire-circle"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_min_value = 200
    _attr_native_max_value = 5000
    _attr_native_step = 100
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._entry = entry
        self._service = entry.runtime_data.ems.water_heater_reserved_service
        self._attr_unique_id = f"{DOMAIN}_ems_water_heater_bonus_gate_on"
        self.entity_id = "number.ems_water_heater_bonus_gate_on"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> float:
        return float(self._service.bonus_gate_on_w)

    async def async_set_native_value(self, value: float) -> None:
        await self._service.set_bonus_gate_on_w(int(value))


class EmsWaterHeaterBonusGateOffNumber(NumberEntity):
    """Bonus threshold (W) to hold the gate open via hysteresis.

    Once heaters are running, they stay on while `export_bonus ≥ this
    threshold` (which should be lower than the ON threshold). See
    `WaterHeaterManager._bonus_gate_open`.
    """

    _attr_has_entity_name = False
    _attr_name = "EMS Water Heater Bonus Gate OFF"
    _attr_should_poll = False
    _attr_icon = "mdi:fire-circle"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_min_value = 0
    _attr_native_max_value = 5000
    _attr_native_step = 100
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._entry = entry
        self._service = entry.runtime_data.ems.water_heater_reserved_service
        self._attr_unique_id = f"{DOMAIN}_ems_water_heater_bonus_gate_off"
        self.entity_id = "number.ems_water_heater_bonus_gate_off"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> float:
        return float(self._service.bonus_gate_off_w)

    async def async_set_native_value(self, value: float) -> None:
        await self._service.set_bonus_gate_off_w(int(value))


class BatteryScheduleSlotTargetSocNumber(NumberEntity):
    """Edit target_soc of a <scope>_<kind> BatterySchedule slot.

    Etap 2E — instantiated for all 8 slots. Range 0-100% with 1% step.

    Validation: `BatteryScheduleEntry.__post_init__` enforces
    `0 <= target_soc <= 100`. UI restricts via min/max, this is the
    aggregate guard.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:battery-charging-50"
    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(
        self, entry: SmartRceConfigEntry, *, scope: Scope, kind: SlotKind
    ) -> None:
        self._entry = entry
        self._service = entry.runtime_data.ems.battery_schedule_service
        self._scope = scope
        self._kind = kind
        slug = f"{scope}_{kind.name.lower()}_target_soc"
        self._attr_name = (
            f"EMS Schedule {scope.title()} "
            f"{kind.name.replace('_', ' ').title()} Target SoC"
        )
        self._attr_unique_id = f"{DOMAIN}_ems_schedule_{slug}"
        self.entity_id = f"number.ems_schedule_{slug}"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> float:
        return self._service.slot(self._scope, self._kind).target_soc

    async def async_set_native_value(self, value: float) -> None:
        await self._service.handle_slot_command(
            SetSlotTargetSocCommand(scope=self._scope, kind=self._kind, value=value)
        )


class OneShotTargetSocNumber(NumberEntity):
    """Edit target_soc param for one-shot operations (per direction).

    Stored in `BatterySchedule._{discharge,charge}_oneshot_params`. Used when
    user presses Execute button — aggregate reads stored params to build the
    OneShotOperation.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:battery-charging-50"
    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: SmartRceConfigEntry, *, direction: Direction) -> None:
        self._entry = entry
        self._service = entry.runtime_data.ems.battery_schedule_service
        self._direction = direction
        slug = f"oneshot_{direction.name.lower()}_target_soc"
        self._attr_name = f"EMS One-Shot {direction.name.title()} Target SoC"
        self._attr_unique_id = f"{DOMAIN}_{slug}"
        self.entity_id = f"number.smart_rce_{slug}"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> float:
        return self._service.oneshot_params(self._direction).target_soc

    async def async_set_native_value(self, value: float) -> None:
        await self._service.handle_oneshot_command(
            SetOneShotTargetSocCommand(direction=self._direction, value=value)
        )
