"""Garden buttons — non-work push + mowing-hold release + manual park.

`button.luba_non_work_push` triggers `NonWorkService.push_to_device()` — the
user-initiated write of the HA target to mammotion. `button.mowing_hold_clear`
triggers `MowingHoldService.clear_hold()` — drops the RAIN hold ("grass is fine,
resume now"; a manual park survives). `button.mowing_park` parks Luba for
`number.mowing_park_minutes` (manual hold, independent of rain);
`button.mowing_park_cancel` drops it. `button.garden_mark_dry` triggers
`RainService.mark_dry()` — the user override "the grass is dry now" that wipes a
false wet reading so the planner + hold stop treating the lawn as wet. Top-level
`button.py` aggregates these via `build_buttons`, so garden owns its presentation.
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
    return [
        LubaNonWorkPushButton(entry),
        MowingHoldClearButton(entry),
        MowingParkButton(entry),
        MowingParkCancelButton(entry),
        GardenMarkDryButton(entry),
    ]


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


class MowingParkButton(ButtonEntity):
    """Press to park Luba for `number.mowing_park_minutes` minutes (manual hold)."""

    _attr_has_entity_name = False
    _attr_name = "Mowing Park"
    _attr_should_poll = False
    _attr_icon = "mdi:pause-circle-outline"

    _PARK_MINUTES_ENTITY = "number.mowing_park_minutes"
    _DEFAULT_MINUTES = 30

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._hold = entry.runtime_data.garden.hold
        self._attr_unique_id = f"{DOMAIN}_mowing_park"
        self.entity_id = "button.mowing_park"
        self._attr_device_info = luba_device_info(entry)

    async def async_press(self) -> None:
        self._hold.park(self._park_minutes())

    def _park_minutes(self) -> int:
        state = self.hass.states.get(self._PARK_MINUTES_ENTITY)
        if state is None:
            return self._DEFAULT_MINUTES
        try:
            return int(float(state.state))
        except (ValueError, TypeError):
            return self._DEFAULT_MINUTES


class MowingParkCancelButton(ButtonEntity):
    """Press to cancel the manual park (rain may still hold)."""

    _attr_has_entity_name = False
    _attr_name = "Mowing Park Cancel"
    _attr_should_poll = False
    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._hold = entry.runtime_data.garden.hold
        self._attr_unique_id = f"{DOMAIN}_mowing_park_cancel"
        self.entity_id = "button.mowing_park_cancel"
        self._attr_device_info = luba_device_info(entry)

    async def async_press(self) -> None:
        self._hold.cancel_park()


class GardenMarkDryButton(ButtonEntity):
    """Press to declare the grass dry now (override a false wet reading)."""

    _attr_has_entity_name = False
    _attr_name = "Garden Mark Dry"
    _attr_should_poll = False
    _attr_icon = "mdi:grass"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._rain = entry.runtime_data.garden.rain
        self._attr_unique_id = f"{DOMAIN}_garden_mark_dry"
        self.entity_id = "button.garden_mark_dry"
        self._attr_device_info = luba_device_info(entry)

    async def async_press(self) -> None:
        self._rain.mark_dry()
