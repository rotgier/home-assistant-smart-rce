"""Garden sensors — mowing planner decision (garden 2b).

`sensor.mowing_planner` exposes the latest `PlannerDecision`: state = start
strategy, attributes = the full decision via `dataclasses.asdict` (descriptive
field names — the legacy Jinja short keys `sh/btt/dk…` were a single-state-string
hack and are intentionally not reproduced). Top-level `sensor/__init__.py`
aggregates these via `build_sensors`, so garden owns its presentation.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from custom_components.smart_rce.const import DOMAIN
from custom_components.smart_rce.garden.domain.mowing_planner import StartStrategy
from custom_components.smart_rce.garden.garden_device import luba_device_info
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity

if TYPE_CHECKING:
    from custom_components.smart_rce import SmartRceConfigEntry


def build_sensors(entry: SmartRceConfigEntry) -> list[SensorEntity]:
    """Garden sensor entities for top-level `sensor` platform to add."""
    return [MowingPlannerSensor(entry)]


class MowingPlannerSensor(SensorEntity):
    """Planner decision: state = strategy, attributes = full PlannerDecision."""

    _attr_has_entity_name = False
    _attr_name = "Mowing Planner"
    _attr_should_poll = False
    _attr_icon = "mdi:robot-mower-outline"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [s.value for s in StartStrategy]

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._service = entry.runtime_data.garden.mowing
        self._attr_unique_id = f"{DOMAIN}_mowing_planner"
        self.entity_id = "sensor.mowing_planner"
        self._attr_device_info = luba_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> str | None:
        decision = self._service.decision
        return decision.strategy.value if decision else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        decision = self._service.decision
        return asdict(decision) if decision else {}
