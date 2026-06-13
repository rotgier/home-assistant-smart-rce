"""Garden buttons — manual non-work push + rain-gate release.

`button.luba_non_work_push` triggers `NonWorkService.push_to_device()` — the
user-initiated write of the HA target to mammotion (the actuator always writes
when a target exists). `button.garden_rain_gate_clear` triggers
`RainGateService.clear_hold()` — drops a rain-gate extension and restores the
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
    return [LubaNonWorkPushButton(entry), GardenRainGateClearButton(entry)]


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


class GardenRainGateClearButton(ButtonEntity):
    """Press to drop a rain-gate extension and resume the target window now."""

    _attr_has_entity_name = False
    _attr_name = "Garden Rain Gate Clear"
    _attr_should_poll = False
    _attr_icon = "mdi:weather-sunny-alert"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._gate = entry.runtime_data.garden.gate
        self._attr_unique_id = f"{DOMAIN}_garden_rain_gate_clear"
        self.entity_id = "button.garden_rain_gate_clear"
        self._attr_device_info = luba_device_info(entry)

    async def async_press(self) -> None:
        self._gate.clear_hold()
