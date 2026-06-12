"""Garden buttons — manual push of the non-work target to the device.

`button.luba_non_work_push` triggers `NonWorkService.push_to_device()` — the
only write path to mammotion in phase 1.5 (user-initiated; the actuator is
state-diff, so pressing with no drift is a no-op). Top-level `button.py`
aggregates these via `build_buttons`, so garden owns its presentation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.smart_rce.const import DOMAIN
from custom_components.smart_rce.ems_device import ems_device_info
from homeassistant.components.button import ButtonEntity

if TYPE_CHECKING:
    from custom_components.smart_rce import SmartRceConfigEntry


def build_buttons(entry: SmartRceConfigEntry) -> list[ButtonEntity]:
    """Garden button entities for top-level `button.py` to add."""
    return [LubaNonWorkPushButton(entry)]


class LubaNonWorkPushButton(ButtonEntity):
    """Press to write the HA non-work target to the mower (state-diff)."""

    _attr_has_entity_name = False
    _attr_name = "Luba Non-Work Push"
    _attr_should_poll = False
    _attr_icon = "mdi:clock-edit-outline"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._service = entry.runtime_data.garden.non_work
        self._attr_unique_id = f"{DOMAIN}_luba_non_work_push"
        self.entity_id = "button.luba_non_work_push"
        self._attr_device_info = ems_device_info(entry)

    async def async_press(self) -> None:
        await self._service.push_to_device()
