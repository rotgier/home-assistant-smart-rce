"""Smart RCE select platform — entities for tri-state user choices.

Currently exposes one entity:
- `select.ems_battery_charge_allowed_override` — three-state override for
  `BatteryChargePolicy.charge_allowed_override`:
    * OFF (passthrough) — schedule + time-gate decides
    * ALLOWED — force charge on
    * DISALLOWED — block charge

Replaces `input_boolean.battery_charge_max_current_toggle` (Etap B migration).
Persistence owned by `BatteryChargeRepository`.
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SmartRceConfigEntry
from .const import DOMAIN
from .domain.battery_charge_policy import OverrideMode
from .domain.battery_schedule import (
    Scope,
    SetSlotBehaviorCommand,
    SlotBehavior,
    SlotKind,
)
from .domain.water_heater_reserved_policy import ReservedMode
from .ems_device import ems_device_info

PARALLEL_UPDATES = 1

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartRceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add smart_rce select entities."""
    async_add_entities(
        [
            EmsBatteryChargeAllowedOverrideSelect(entry),
            EmsWaterHeaterReservedModeSelect(entry),
            *[
                BatteryScheduleSlotBehaviorSelect(entry, scope=scope, kind=kind)
                for scope in ("today", "tomorrow")
                for kind in SlotKind
            ],
        ]
    )


class EmsBatteryChargeAllowedOverrideSelect(SelectEntity):
    """User-controlled override of battery charge enablement.

    Bridge to `BatteryChargePolicy.charge_allowed_override` via
    `BatteryChargeService`. The three values let the user:
    - OFF: let smart_rce decide (default — based on schedule + time-gate)
    - ALLOWED: force charging on (override schedule)
    - DISALLOWED: force charging off (block schedule)

    Persistence handled by `BatteryChargeRepository` (own Store).
    """

    _attr_has_entity_name = False
    _attr_name = "EMS Battery Charge Allowed Override"
    _attr_should_poll = False
    _attr_icon = "mdi:battery-charging-outline"
    _attr_options = [m.value for m in OverrideMode]

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._entry = entry
        self._service = entry.runtime_data.ems.battery_charge_service
        self._attr_unique_id = f"{DOMAIN}_ems_battery_charge_allowed_override"
        self.entity_id = "select.ems_battery_charge_allowed_override"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def current_option(self) -> str:
        return self._service.charge_allowed_override.value

    async def async_select_option(self, option: str) -> None:
        # HA platform validates `option` against `_attr_options` before calling us.
        await self._service.set_charge_allowed_override(OverrideMode(option))


class EmsWaterHeaterReservedModeSelect(SelectEntity):
    """Mode switch for water-heater reserved-power policy (AUTO / MANUAL).

    - AUTO: policy.compute_current_value(now, input) drives the value
      (currently stub = 3000; future: dynamic logic based on RCE prices +
      PV forecast + weather).
    - MANUAL: user-set value via `number.ems_water_heater_reserved` is used.

    Persistence handled by `WaterHeaterReservedRepository` (own Store).
    """

    _attr_has_entity_name = False
    _attr_name = "EMS Water Heater Reserved Mode"
    _attr_should_poll = False
    _attr_icon = "mdi:water-boiler-auto"
    _attr_options = [m.value for m in ReservedMode]

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._entry = entry
        self._service = entry.runtime_data.ems.water_heater_reserved_service
        self._attr_unique_id = f"{DOMAIN}_ems_water_heater_reserved_mode"
        self.entity_id = "select.ems_water_heater_reserved_mode"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def current_option(self) -> str:
        return self._service.mode.value

    async def async_select_option(self, option: str) -> None:
        await self._service.set_mode(ReservedMode(option))


class BatteryScheduleSlotBehaviorSelect(SelectEntity):
    """Per-slot SlotBehavior (IMMEDIATE / DELAYED_TO_END).

    IMMEDIATE — engage at `start` once in-window and target not reached.
    DELAYED_TO_END — engage "just-in-time" so target_soc is reached just
    before `end`. Default for newly-created slots (energy-efficient — battery
    spends less time at extreme SoC).

    Used for slot-level adaptive engagement timing. E.g. evening peak
    discharge with DELAYED_TO_END starts late enough to finish target SoC
    at end of peak hour window, minimizing time at 100% SoC.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:clock-fast"
    _attr_options = [b.value for b in SlotBehavior]

    def __init__(
        self, entry: SmartRceConfigEntry, *, scope: Scope, kind: SlotKind
    ) -> None:
        self._entry = entry
        self._service = entry.runtime_data.ems.battery_schedule_service
        self._scope = scope
        self._kind = kind
        slug = f"{scope}_{kind.name.lower()}_behavior"
        self._attr_name = (
            f"EMS Schedule {scope.title()} "
            f"{kind.name.replace('_', ' ').title()} Behavior"
        )
        self._attr_unique_id = f"{DOMAIN}_ems_schedule_{slug}"
        self.entity_id = f"select.ems_schedule_{slug}"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def current_option(self) -> str:
        return self._service.slot(self._scope, self._kind).behavior.value

    async def async_select_option(self, option: str) -> None:
        await self._service.handle_slot_command(
            SetSlotBehaviorCommand(
                scope=self._scope, kind=self._kind, value=SlotBehavior(option)
            )
        )
