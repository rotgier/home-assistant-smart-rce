"""Smart RCE time platform — entities for time-of-day settings.

Currently exposes one entity:
- `time.ems_battery_charge_start_hour_override` — view of
  `BatteryChargePolicy.start_charge_hour_override`. Drives the morning
  block window `[06:00, start_charge_hour_override)` in
  `BatteryChargePolicy.charge_allowed`.
- `time.ems_battery_charge_start_hour_tomorrow` — tomorrow's start: the
  manual plan when set, otherwise the window computed from tomorrow's RCE
  prices. Lets the evening question "is tomorrow set up sensibly?" be both
  answered and acted on; the plan is promoted into the entity above on the
  day it names.

Replaces legacy `input_datetime.rce_start_charge_hour_today_override`
(Etap B'-2 migration). Persistence owned by `BatteryChargeRepository`.
"""

from __future__ import annotations

from datetime import time
import logging

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SmartRceConfigEntry
from .const import DOMAIN, ICON_AUTOMATIC, ICON_HAND_SET
from .domain.battery_schedule import (
    Direction,
    Scope,
    SetOneShotEndTimeCommand,
    SetSlotEndCommand,
    SetSlotStartCommand,
    SlotKind,
)
from .ems_device import ems_device_info
from .garden.time_entities import build_times

PARALLEL_UPDATES = 1

# Charge-start entities swap their icon by provenance, so a glance at the card
# tells a hand-set time from one the RCE window produced.
ICON_START_AUTO = ICON_AUTOMATIC
ICON_START_MANUAL = ICON_HAND_SET

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartRceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add smart_rce time entities."""
    time_fields: list[tuple[str, type[SetSlotStartCommand | SetSlotEndCommand]]] = [
        ("start", SetSlotStartCommand),
        ("end", SetSlotEndCommand),
    ]
    scopes: tuple[Scope, ...] = ("today", "tomorrow")
    async_add_entities(
        [
            EmsBatteryChargeStartHourOverrideTime(entry),
            EmsBatteryChargeStartHourTomorrowTime(entry),
            *[
                BatteryScheduleSlotTime(
                    entry,
                    scope=scope,
                    kind=kind,
                    field=field,
                    command_cls=command_cls,
                )
                for scope in scopes
                for kind in SlotKind
                for field, command_cls in time_fields
            ],
            OneShotEndTime(entry, direction=Direction.DISCHARGE),
            OneShotEndTime(entry, direction=Direction.CHARGE),
            *build_times(entry),
        ]
    )


class EmsBatteryChargeStartHourOverrideTime(TimeEntity):
    """Morning charge window start.

    Toggle of `BatteryChargePolicy.charge_allowed` is OFF in the block
    window `[06:00, native_value)` and ON elsewhere. None disables the
    time-gate (defers to schedule).
    """

    _attr_has_entity_name = False
    _attr_name = "EMS Battery Charge Start Hour Override"
    _attr_should_poll = False

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._entry = entry
        self._service = entry.runtime_data.ems.battery_charge_service
        self._attr_unique_id = f"{DOMAIN}_ems_battery_charge_start_hour_override"
        self.entity_id = "time.ems_battery_charge_start_hour_override"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> time | None:
        return self._service.start_charge_hour_override

    @property
    def icon(self) -> str:
        return _start_icon(self._service.start_charge_is_manual_today)

    async def async_set_value(self, value: time) -> None:
        await self._service.set_start_charge_hour_manual(value)


class EmsBatteryChargeStartHourTomorrowTime(TimeEntity):
    """Tomorrow's charge-window start — computed by default, overridable.

    Reads through `Ems`, which joins the manual plan held by
    `BatteryChargePolicy` with the window computed from tomorrow's RCE
    prices. `unknown` until those prices are published (~14:00).

    Setting a value pins it to tomorrow's date; `refresh_start_charge`
    promotes it into `start_charge_hour_override` when that day arrives —
    by comparing dates, so it survives a restart or an HA outage across
    midnight.
    """

    _attr_has_entity_name = False
    _attr_name = "EMS Battery Charge Start Hour Tomorrow"
    _attr_should_poll = False

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._entry = entry
        self._ems = entry.runtime_data.ems
        self._attr_unique_id = f"{DOMAIN}_ems_battery_charge_start_hour_tomorrow"
        self.entity_id = "time.ems_battery_charge_start_hour_tomorrow"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._ems.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> time | None:
        return self._ems.charge_start_tomorrow

    @property
    def icon(self) -> str:
        return _start_icon(self._ems.charge_start_tomorrow_is_manual)

    async def async_set_value(self, value: time) -> None:
        await self._ems.set_charge_start_tomorrow(value)


def _start_icon(is_manual: bool) -> str:
    """Icon for a charge-start entity — shared by the today and tomorrow views."""
    return ICON_START_MANUAL if is_manual else ICON_START_AUTO


class BatteryScheduleSlotTime(TimeEntity):
    """Edit start/end time of a <scope>_<kind> BatterySchedule slot.

    Etap 2E — instantiated for all 8 slots × 2 fields = 16 time entities.
    `field` ∈ {"start", "end"} — determines read-side label/getattr and
    pairs with the appropriate Command class (`SetSlotStartCommand` /
    `SetSlotEndCommand`).

    Validation: `BatteryScheduleEntry.__post_init__` enforces
    `start < end` when `enabled=True`. ValueError propagates to HA
    service call failure if user tries to set end <= start while slot
    is enabled.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:clock-outline"

    def __init__(
        self,
        entry: SmartRceConfigEntry,
        *,
        scope: Scope,
        kind: SlotKind,
        field: str,
        command_cls: type[SetSlotStartCommand | SetSlotEndCommand],
    ) -> None:
        self._entry = entry
        self._service = entry.runtime_data.ems.battery_schedule_service
        self._scope = scope
        self._kind = kind
        self._field = field  # "start" or "end" — read-side label + getattr
        self._command_cls = command_cls
        slug = f"{scope}_{kind.name.lower()}_{field}"
        self._attr_name = (
            f"EMS Schedule {scope.title()} "
            f"{kind.name.replace('_', ' ').title()} {field.capitalize()}"
        )
        self._attr_unique_id = f"{DOMAIN}_ems_schedule_{slug}"
        self.entity_id = f"time.ems_schedule_{slug}"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> time | None:
        value: time | None = getattr(
            self._service.slot(self._scope, self._kind), self._field
        )
        return value

    async def async_set_value(self, value: time) -> None:
        await self._service.handle_slot_command(
            self._command_cls(scope=self._scope, kind=self._kind, value=value)
        )


class OneShotEndTime(TimeEntity):
    """Edit end_time param for one-shot operations (per direction).

    Time-of-day; aggregate combines with `now.date()` when user presses
    Execute. If end_time <= now.time(), aggregate rolls to next day (e.g.
    discharge until 06:00 started at 22:00 ends tomorrow 06:00).
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:clock-end"

    def __init__(self, entry: SmartRceConfigEntry, *, direction: Direction) -> None:
        self._entry = entry
        self._service = entry.runtime_data.ems.battery_schedule_service
        self._direction = direction
        slug = f"oneshot_{direction.name.lower()}_end_time"
        self._attr_name = f"EMS One-Shot {direction.name.title()} End Time"
        self._attr_unique_id = f"{DOMAIN}_{slug}"
        self.entity_id = f"time.smart_rce_{slug}"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> time:
        return self._service.oneshot_params(self._direction).end_time

    async def async_set_value(self, value: time) -> None:
        await self._service.handle_oneshot_command(
            SetOneShotEndTimeCommand(direction=self._direction, value=value)
        )
