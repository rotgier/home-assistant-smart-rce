"""Shared `EMS` HA device info — used by all bounded-context entities.

All smart_rce-owned EMS entities (binary_sensor / sensor / select / time /
switch / number / ...) share a single virtual HA device named `EMS` so the
config/entities/devices view groups them together. This helper keeps the
DeviceInfo dict in one place — change it here, all entities pick it up.

Identifier: ("ems", entry.entry_id) — stable across restarts (entry_id is
persistent), separate per config entry (multiple smart_rce instances
would each get their own EMS device).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

if TYPE_CHECKING:
    from . import SmartRceConfigEntry


def ems_device_info(entry: SmartRceConfigEntry) -> DeviceInfo:
    """Return shared EMS DeviceInfo for the given config entry."""
    return DeviceInfo(
        name="EMS",
        identifiers={("ems", entry.entry_id)},
        entry_type=DeviceEntryType.SERVICE,
    )
