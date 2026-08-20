"""Shared HA device for deposit-context entities.

Own device rather than EMS's: the deposit context reports on settlement, not on
energy control, and grouping the two would suggest a coupling that ADR-025
deliberately avoids.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DEPOSIT_UNIQUE_ID_PREFIX, DEVICE_NAME

if TYPE_CHECKING:
    from custom_components.smart_rce import SmartRceConfigEntry


def deposit_device_info(entry: SmartRceConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        name=DEVICE_NAME,
        identifiers={(DEPOSIT_UNIQUE_ID_PREFIX, entry.entry_id)},
        entry_type=DeviceEntryType.SERVICE,
    )
