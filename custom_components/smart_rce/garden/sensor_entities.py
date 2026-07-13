"""Garden sensors — mowing planner decision + grass dry-out time.

`sensor.mowing_planner` exposes the latest `PlannerDecision`: state = start
strategy, attributes = the full decision via `dataclasses.asdict` (descriptive
field names — the legacy Jinja short keys `sh/btt/dk…` were a single-state-string
hack and are intentionally not reproduced). Each decision field that benefits
from its own history is also published as a standalone sensor
(`MowingPlannerFieldSensor`) — attribute-only changes are filtered by the history
API (`significant_changes_only`), so attributes graph poorly. `sensor.garden_dry_at`
exposes when the grass is dry enough to mow (`RainService.dry_at`). Top-level
`sensor/__init__.py` aggregates these via `build_sensors`, so garden owns its
presentation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from custom_components.smart_rce.const import DOMAIN
from custom_components.smart_rce.garden.domain.forecast_window import WindowBound
from custom_components.smart_rce.garden.domain.mowing_planner import StartStrategy
from custom_components.smart_rce.garden.garden_device import luba_device_info
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTime

if TYPE_CHECKING:
    from datetime import datetime

    from custom_components.smart_rce import SmartRceConfigEntry
    from custom_components.smart_rce.garden.domain.mowing_planner import PlannerDecision


def build_sensors(entry: SmartRceConfigEntry) -> list[SensorEntity]:
    """Garden sensor entities for top-level `sensor` platform to add."""
    sensors: list[SensorEntity] = [
        MowingPlannerSensor(entry),
        GardenDryAtSensor(entry),
        MowingNonWorkStatusSensor(entry),
    ]
    sensors.extend(MowingPlannerFieldSensor(entry, field) for field in _PLANNER_FIELDS)
    return sensors


class MowingNonWorkStatusSensor(SensorEntity):
    """What the device non-work window currently reflects.

    Disambiguates the drift alert: when a hold is active the device window
    deliberately differs from the user target (expected), so this names the
    reason instead of looking like unexplained drift.
    - `target`      — device matches the user target (normal).
    - `rain_hold`   — overridden by the rain hold.
    - `manual_hold` — overridden by a manual park.
    - `drift`       — device ≠ target with NO hold (unexpected — investigate).
    - `unknown`     — no target set yet / device state unknown.
    """

    _attr_has_entity_name = False
    _attr_name = "Mowing Non-Work Status"
    _attr_should_poll = False
    _attr_icon = "mdi:clock-check-outline"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["target", "rain_hold", "manual_hold", "drift", "unknown"]

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._non_work = entry.runtime_data.garden.non_work
        self._hold = entry.runtime_data.garden.hold
        self._attr_unique_id = f"{DOMAIN}_mowing_non_work_status"
        self.entity_id = "sensor.mowing_non_work_status"
        self._attr_device_info = luba_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._non_work.add_listener(self.async_write_ha_state))
        self.async_on_remove(self._hold.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> str:
        if self._hold.is_manual_parked:
            return "manual_hold"
        if self._hold.is_holding:
            return "rain_hold"
        if self._non_work.effective_hours is None:
            return "unknown"
        return "drift" if self._non_work.drift else "target"


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


class MowingPlannerFieldSensor(SensorEntity):
    """One derived field of the latest PlannerDecision, with its own history.

    Mirrors a `sensor.mowing_planner` attribute as a standalone sensor so it gets
    long-term statistics / graphable history (attribute-only changes are dropped
    by the history API). Battery/progress/at_dock are omitted — they already have
    native mammotion sensors — and strategy is the parent sensor's own state.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, entry: SmartRceConfigEntry, field: _PlannerField) -> None:
        self._service = entry.runtime_data.garden.mowing
        self._field = field
        self._attr_name = field.name
        self._attr_unique_id = f"{DOMAIN}_{field.key}"
        self.entity_id = f"sensor.{field.key}"
        self._attr_device_info = luba_device_info(entry)
        self._attr_icon = field.icon
        self._attr_device_class = field.device_class
        self._attr_state_class = field.state_class
        self._attr_native_unit_of_measurement = field.unit
        if field.options is not None:
            self._attr_options = field.options

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> Any:
        decision = self._service.decision
        return self._field.value(decision) if decision else None


@dataclass(frozen=True)
class _PlannerField:
    """Presentation spec mapping a PlannerDecision field to a sensor."""

    key: str  # entity_id + unique_id suffix
    name: str
    value: Callable[[PlannerDecision], Any]
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    unit: str | None = None
    options: list[str] | None = None
    icon: str | None = None


_MIN = UnitOfTime.MINUTES
_DURATION = SensorDeviceClass.DURATION
_MEASUREMENT = SensorStateClass.MEASUREMENT
_TIMESTAMP = SensorDeviceClass.TIMESTAMP

_PLANNER_FIELDS: tuple[_PlannerField, ...] = (
    _PlannerField(
        "mowing_window_start",
        "Mowing Window Start",
        lambda d: d.window_start,
        device_class=_TIMESTAMP,
        icon="mdi:clock-start",
    ),
    _PlannerField(
        "mowing_window_end",
        "Mowing Window End",
        lambda d: d.window_end,
        device_class=_TIMESTAMP,
        icon="mdi:clock-end",
    ),
    _PlannerField(
        "mowing_opt_start",
        "Mowing Optimal Start",
        lambda d: d.opt_start,
        device_class=_TIMESTAMP,
        icon="mdi:clock-check-outline",
    ),
    _PlannerField(
        "mowing_window_bound",
        "Mowing Window Bound",
        lambda d: d.window_bound.value,
        device_class=SensorDeviceClass.ENUM,
        options=[b.value for b in WindowBound],
        icon="mdi:window-shutter",
    ),
    _PlannerField(
        "mowing_window_min",
        "Mowing Window Minutes",
        lambda d: d.window_min,
        device_class=_DURATION,
        state_class=_MEASUREMENT,
        unit=_MIN,
        icon="mdi:timer-sand",
    ),
    _PlannerField(
        "mowing_needed_min",
        "Mowing Needed Minutes",
        lambda d: d.needed_min,
        device_class=_DURATION,
        state_class=_MEASUREMENT,
        unit=_MIN,
        icon="mdi:timer-sand",
    ),
    _PlannerField(
        "mowing_time_to_drain",
        "Mowing Time To Drain",
        lambda d: d.time_to_drain_min,
        device_class=_DURATION,
        state_class=_MEASUREMENT,
        unit=_MIN,
        icon="mdi:battery-arrow-down-outline",
    ),
    _PlannerField(
        "mowing_time_to_finish",
        "Mowing Time To Finish",
        lambda d: d.time_to_finish_min,
        device_class=_DURATION,
        state_class=_MEASUREMENT,
        unit=_MIN,
        icon="mdi:flag-checkered",
    ),
)


class GardenDryAtSensor(SensorEntity):
    """When the grass is dry enough to mow (rain end + dry-out hours)."""

    _attr_has_entity_name = False
    _attr_name = "Garden Dry At"
    _attr_should_poll = False
    _attr_icon = "mdi:weather-sunny"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._service = entry.runtime_data.garden.rain
        self._attr_unique_id = f"{DOMAIN}_garden_dry_at"
        self.entity_id = "sensor.garden_dry_at"
        self._attr_device_info = luba_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> datetime | None:
        return self._service.dry_at
