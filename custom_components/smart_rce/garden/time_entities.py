"""Garden time entities — non-work window start/end (target = HA source of truth).

Two `time` entities backed by `NonWorkService` (which owns the target via the
repo; observe-first — no device writes, drift is surfaced via
`binary_sensor.luba_non_work_drift`). They subscribe to the service so any
change refreshes them. First-time setup: both edges start empty; entering one
shows as a pending value until the other completes the target. Top-level
`time.py` aggregates these via `build_times` (Decyzja #8 contract), so garden
owns its presentation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.smart_rce.const import DOMAIN
from custom_components.smart_rce.ems_device import ems_device_info
from homeassistant.components.time import TimeEntity

if TYPE_CHECKING:
    from datetime import time

    from custom_components.smart_rce import SmartRceConfigEntry


def build_times(entry: SmartRceConfigEntry) -> list[TimeEntity]:
    """Garden time entities for top-level `time.py` to add."""
    return [
        LubaNonWorkTime(entry, field="start"),
        LubaNonWorkTime(entry, field="end"),
    ]


class LubaNonWorkTime(TimeEntity):
    """Start/end of the non-work (quiet) window — garden-owned target."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:clock-outline"

    def __init__(self, entry: SmartRceConfigEntry, *, field: str) -> None:
        self._service = entry.runtime_data.garden.non_work
        self._field = field  # "start" or "end"
        self._attr_name = f"Luba Non-Work {field.capitalize()}"
        self._attr_unique_id = f"{DOMAIN}_luba_non_work_{field}"
        self.entity_id = f"time.luba_non_work_{field}"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> time | None:
        return self._service.start if self._field == "start" else self._service.end

    async def async_set_value(self, value: time) -> None:
        if self._field == "start":
            await self._service.set_start(value)
        else:
            await self._service.set_end(value)
