"""Garden buttons — manual non-work push + mowing-hold release.

`button.luba_non_work_push` triggers `NonWorkService.push_to_device()` — the
user-initiated write of the HA target to mammotion (the actuator always writes
when a target exists). `button.mowing_hold_clear` triggers
`MowingHoldService.clear_hold()` — drops the mowing hold and restores the
target ("grass is fine, resume now"). Top-level `button.py` aggregates these
via `build_buttons`, so garden owns its presentation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.smart_rce.const import DOMAIN
from custom_components.smart_rce.garden.garden_device import luba_device_info
from homeassistant.components.button import ButtonEntity

if TYPE_CHECKING:
    from custom_components.smart_rce import SmartRceConfigEntry


def build_buttons(entry: SmartRceConfigEntry) -> list[ButtonEntity]:
    """Garden button entities for top-level `button.py` to add."""
    return [LubaNonWorkPushButton(entry), MowingHoldClearButton(entry)]


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
        self._attr_device_info = luba_device_info(entry)

    async def async_press(self) -> None:
        await self._service.push_to_device()


class MowingHoldClearButton(ButtonEntity):
    """Press to drop the mowing hold and resume the target window now."""

    _attr_has_entity_name = False
    _attr_name = "Mowing Hold Clear"
    _attr_should_poll = False
    _attr_icon = "mdi:weather-sunny-alert"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._hold = entry.runtime_data.garden.hold
        self._attr_unique_id = f"{DOMAIN}_mowing_hold_clear"
        self.entity_id = "button.mowing_hold_clear"
        self._attr_device_info = luba_device_info(entry)

    async def async_press(self) -> None:
        self._hold.clear_hold()
