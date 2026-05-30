"""Smart RCE button platform — one-shot operation triggers.

Etap 2F — 4 buttons per direction (charge / discharge):
- `button.smart_rce_oneshot_<dir>_execute` — starts one-shot using stored
  params (target_soc + end_time from number/time entities)
- `button.smart_rce_oneshot_<dir>_cancel` — cancels active one-shot

Persistence owned by `BatteryScheduleRepository`.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SmartRceConfigEntry
from .const import DOMAIN
from .domain.battery_schedule import CHARGE, DISCHARGE, Direction
from .ems_device import ems_device_info

PARALLEL_UPDATES = 1

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartRceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add smart_rce button entities."""
    async_add_entities(
        [
            OneShotExecuteButton(entry, direction=DISCHARGE),
            OneShotCancelButton(entry, direction=DISCHARGE),
            OneShotExecuteButton(entry, direction=CHARGE),
            OneShotCancelButton(entry, direction=CHARGE),
        ]
    )


class OneShotExecuteButton(ButtonEntity):
    """Press to start a one-shot operation using stored params.

    Reads `target_soc` and `end_time` from companion number/time entities
    (per direction). No-op if a one-shot is already active (aggregate
    enforces single-active invariant).
    """

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, entry: SmartRceConfigEntry, *, direction: Direction) -> None:
        self._entry = entry
        self._service = entry.runtime_data.ems.battery_schedule_service
        self._direction = direction
        slug = f"oneshot_{direction.name.lower()}_execute"
        self._attr_name = f"EMS One-Shot {direction.name.title()} Execute"
        self._attr_unique_id = f"{DOMAIN}_{slug}"
        self.entity_id = f"button.smart_rce_{slug}"
        self._attr_icon = (
            "mdi:battery-arrow-down"
            if direction.is_discharge
            else "mdi:battery-arrow-up"
        )
        self._attr_device_info = ems_device_info(entry)

    async def async_press(self) -> None:
        await self._service.handle_start_oneshot(self._direction)


class OneShotCancelButton(ButtonEntity):
    """Press to cancel an active one-shot operation. No-op when idle."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:stop-circle-outline"

    def __init__(self, entry: SmartRceConfigEntry, *, direction: Direction) -> None:
        self._entry = entry
        self._service = entry.runtime_data.ems.battery_schedule_service
        self._direction = direction
        slug = f"oneshot_{direction.name.lower()}_cancel"
        self._attr_name = f"EMS One-Shot {direction.name.title()} Cancel"
        self._attr_unique_id = f"{DOMAIN}_{slug}"
        self.entity_id = f"button.smart_rce_{slug}"
        self._attr_device_info = ems_device_info(entry)

    async def async_press(self) -> None:
        # Cancel applies regardless of which button is pressed — direction is
        # only used for entity identity. Aggregate stores only one active op.
        await self._service.handle_cancel_oneshot()
