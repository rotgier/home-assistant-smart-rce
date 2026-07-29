"""Garden switches — service (maintenance) mode toggle.

`switch.garden_service_mode` flips `ServiceModeService`: ON suppresses garden
auto-actions (planner `should_start` → False, so the auto-dispatch automation
never fires; mowing hold skips non-work pushes) and gates the Luba notification
automations so they do not spam while the user handles the mower. Top-level
`switch.py` aggregates these via `build_switches`, so garden owns its
presentation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.smart_rce.const import DOMAIN
from custom_components.smart_rce.garden.garden_device import luba_device_info
from homeassistant.components.switch import SwitchEntity

if TYPE_CHECKING:
    from custom_components.smart_rce import SmartRceConfigEntry


def build_switches(entry: SmartRceConfigEntry) -> list[SwitchEntity]:
    """Garden switch entities for top-level `switch.py` to add."""
    return [GardenServiceModeSwitch(entry)]


class GardenServiceModeSwitch(SwitchEntity):
    """Maintenance mode — ON suppresses garden auto-actions + notification spam."""

    _attr_has_entity_name = False
    _attr_name = "Garden Service Mode"
    _attr_should_poll = False
    _attr_icon = "mdi:wrench-clock"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._service = entry.runtime_data.garden.service_mode
        self._attr_unique_id = f"{DOMAIN}_garden_service_mode"
        self.entity_id = "switch.garden_service_mode"
        self._attr_device_info = luba_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self._service.is_active

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._service.set_active(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._service.set_active(False)
