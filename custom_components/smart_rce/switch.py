"""Smart RCE switch platform — entities that view smart_rce domain state.

Currently exposes one entity:
- `switch.ems_interventions_blocked` — view of
  `BatterySchedule._interventions_blocked_override` (the user-controlled part
  of the combined `ems_interventions_blocked` property). User toggle mutates
  this field via `repo.set_interventions_blocked_override`.

Replaces legacy `input_boolean.ems_allow_discharge_override` (Etap 0
migration). Persistence is owned by `BatteryScheduleRepository` (own Store
with SAVE_DELAY=0 → ~1s crash safety vs RestoreStateData's 15-min cycle).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    """Add smart_rce switch entities."""
    async_add_entities([EmsInterventionsBlockedSwitch(entry)])


class EmsInterventionsBlockedSwitch(SwitchEntity):
    """View + UI bridge for the user-controlled part of EMS interventions block.

    Represents `BatterySchedule._interventions_blocked_override` — the field
    the user toggles to force smart_rce's interventions off regardless of
    schedule engagement. The combined effect (override OR active engagement)
    lives in `schedule.ems_interventions_blocked` and is read directly by
    DodPolicy / GridExportManager in Ems.update_state.

    Why only the user part (not combined):
    - If the switch exposed the combined value, toggling during active
      engagement would have no visible effect (combined stays True even after
      user toggles off, because engagement still drives it).
    - User-only semantics → switch is "force-on" — predictable, no confusion
      between "I'm forcing" and "schedule happens to be active".
    - Schedule engagement surfaced separately (dashboard cards or future
      diagnostic sensor in Etap 2A).

    Persistence: handled by `BatteryScheduleRepository` (own Store with
    SAVE_DELAY=0). Restored at startup via `repo.async_restore()` before any
    entity initialization, so `is_on` reflects persisted state immediately.
    """

    _attr_has_entity_name = False
    _attr_name = "EMS Interventions Blocked"
    _attr_should_poll = False
    _attr_icon = "mdi:shield-off"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._entry = entry
        self._service = entry.runtime_data.ems.battery_schedule_service
        self._attr_unique_id = f"{DOMAIN}_ems_interventions_blocked"
        # Forces entity_id to switch.ems_interventions_blocked
        self.entity_id = "switch.ems_interventions_blocked"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Subscribe to service state changes for UI refresh."""
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        """User-controlled override only (not combined with schedule engagement)."""
        return self._service.ems_interventions_blocked_override

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._service.set_ems_interventions_blocked_override(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._service.set_ems_interventions_blocked_override(False)
