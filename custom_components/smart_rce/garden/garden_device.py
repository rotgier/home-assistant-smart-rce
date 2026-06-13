"""Shared `Luba` HA device info — used by all garden-context entities.

Garden-owned entities (mowing planner, non-work target/drift, push button)
share a single virtual HA device named `Luba`, separate from the `EMS` device
(`ems_device.py`) — garden is its own bounded context (ADR-024), so its
entities group under their own device rather than under EMS.

Distinct from the mammotion integration's own `Luba-MN9XCNVU` device: this one
holds smart_rce's derived/control entities; identifier `("garden", entry_id)`
keeps it stable across restarts and separate per config entry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

if TYPE_CHECKING:
    from custom_components.smart_rce import SmartRceConfigEntry


def luba_device_info(entry: SmartRceConfigEntry) -> DeviceInfo:
    """Return shared Luba (garden) DeviceInfo for the given config entry."""
    return DeviceInfo(
        name="Luba",
        identifiers={("garden", entry.entry_id)},
        entry_type=DeviceEntryType.SERVICE,
    )
