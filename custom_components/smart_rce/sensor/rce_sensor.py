"""SmartRceSensor — RCE prices + charge/discharge slots from Ems."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Any, Final

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfTime
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.dt import now as now_local

from ..application.ems import Ems
from ..const import DOMAIN, GROSS_MULTIPLIER
from ..coordinator import SmartRceDataUpdateCoordinator
from ._state_writer_mixin import StateWriterMixin

CURRENCY_PLN: Final = "zł"
UNIQUE_ID_PREFIX = DOMAIN

_LOGGER = logging.getLogger(__name__)


class SmartRceSensor(
    CoordinatorEntity[SmartRceDataUpdateCoordinator], StateWriterMixin, RestoreSensor
):
    """Sensor reading current/historical RCE prices + charge/discharge slots from Ems."""

    _attr_has_entity_name = True
    entity_description: SmartRceSensorDescription

    def __init__(
        self,
        coordinator: SmartRceDataUpdateCoordinator,
        ems: Ems,
        description: SmartRceSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.ems: Ems = ems
        self.entity_description = description
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_{description.key}"
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        if self.entity_description.restore_fn:
            last_state = await self.async_get_last_state()
            if last_state:
                self.entity_description.restore_fn(self.ems, last_state.attributes)

        self._register_state_writer(self.ems)
        self._handle_coordinator_update()
        _LOGGER.debug(
            "Setup of RCE Smart sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @property
    def native_value(self) -> str | int | float | datetime | None:
        return self.entity_description.value_fn(self.ems)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.entity_description.attr_fn(self.ems)


@dataclass(frozen=True, kw_only=True)
class SmartRceSensorDescription(SensorEntityDescription):
    """Description schema for SmartRceSensor — value_fn/attr_fn lambdas + optional restore_fn."""

    key: str = field(init=False)
    value_fn: Callable[[Ems], str | int | float | datetime | None]
    attr_fn: Callable[[Ems], dict[str, Any]] = lambda _: {}
    restore_fn: Callable[[Ems, dict[str, Any]], None] | None = None

    def __post_init__(self) -> None:
        assert isinstance(self.name, str)
        object.__setattr__(self, "key", self.name.lower().replace(" ", "_"))


def _avg_price(ems: Ems, day: str) -> float | None:
    """Thin wrapper — delegate to RceDayPrices.avg_price domain property."""
    rce_data = ems.rce_prices.rce_prices
    if not rce_data:
        return None
    day_prices = rce_data.today if day == "today" else rce_data.tomorrow
    return day_prices.avg_price if day_prices else None


def _prices_attr(ems: Ems, day: str) -> dict[str, Any]:
    rce_data = ems.rce_prices.rce_prices
    if not rce_data:
        return {}
    day_prices = rce_data.today if day == "today" else rce_data.tomorrow
    if not day_prices or not day_prices.hour_price:
        return {}
    return {
        "prices": [
            {
                "datetime": day_prices.datetime_at_hour(hour).isoformat(),
                "price": price,
            }
            for hour, price in enumerate(day_prices.hour_price)
        ]
    }


def _restore_prices_today(ems: Ems, attrs: dict[str, Any]) -> None:
    prices = attrs.get("prices")
    if prices:
        ems.restore_rce_today(prices, now_local())


def _restore_prices_tomorrow(ems: Ems, attrs: dict[str, Any]) -> None:
    prices = attrs.get("prices")
    if prices:
        ems.restore_rce_tomorrow(prices, now_local())


SENSOR_DESCRIPTIONS: tuple[SmartRceSensorDescription, ...] = (
    SmartRceSensorDescription(
        name="Current Price",
        native_unit_of_measurement=f"{CURRENCY_PLN}/{UnitOfEnergy.MEGA_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.rce_prices.current_price,
    ),
    SmartRceSensorDescription(
        name="Prices Today",
        native_unit_of_measurement=f"{CURRENCY_PLN}/{UnitOfEnergy.MEGA_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: _avg_price(ems, "today"),
        attr_fn=lambda ems: _prices_attr(ems, "today"),
        restore_fn=_restore_prices_today,
    ),
    SmartRceSensorDescription(
        name="Prices Tomorrow",
        native_unit_of_measurement=f"{CURRENCY_PLN}/{UnitOfEnergy.MEGA_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: _avg_price(ems, "tomorrow"),
        attr_fn=lambda ems: _prices_attr(ems, "tomorrow"),
        restore_fn=_restore_prices_tomorrow,
    ),
    SmartRceSensorDescription(
        name="Max Upcoming Peak Gross",
        native_unit_of_measurement=f"{CURRENCY_PLN}/{UnitOfEnergy.MEGA_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:cash-clock",
        value_fn=lambda ems: (
            round(ems.discharge_slots.max_upcoming_peak.price * GROSS_MULTIPLIER, 2)
            if ems.discharge_slots.max_upcoming_peak
            else None
        ),
    ),
    SmartRceSensorDescription(
        name="Max Upcoming Peak Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-outline",
        value_fn=lambda ems: (
            ems.discharge_slots.max_upcoming_peak.datetime
            if ems.discharge_slots.max_upcoming_peak
            else None
        ),
    ),
    SmartRceSensorDescription(
        name="Morning Discharge Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:weather-sunset-up",
        value_fn=lambda ems: (
            ems.discharge_slots.best_morning_discharge_slot.datetime
            if ems.discharge_slots.best_morning_discharge_slot
            else None
        ),
    ),
    SmartRceSensorDescription(
        name="Morning Discharge Price",
        native_unit_of_measurement=f"{CURRENCY_PLN}/{UnitOfEnergy.MEGA_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:cash-clock",
        value_fn=lambda ems: (
            round(
                ems.discharge_slots.best_morning_discharge_slot.price
                * GROSS_MULTIPLIER,
                2,
            )
            if ems.discharge_slots.best_morning_discharge_slot
            else None
        ),
    ),
    # --- Today charge slots ---
    SmartRceSensorDescription(
        name="Start Charge Hour Today",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.charge_slots.today.start_hour
        if ems.charge_slots.today
        else None,
    ),
    SmartRceSensorDescription(
        name="Start Charge Hour Today Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.charge_slots.today.start_datetime
        if ems.charge_slots.today
        else None,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Today",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.charge_slots.today.end_hour
        if ems.charge_slots.today
        else None,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Today Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.charge_slots.today.end_datetime
        if ems.charge_slots.today
        else None,
    ),
    # --- Tomorrow charge slots ---
    SmartRceSensorDescription(
        name="Start Charge Hour Tomorrow",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.charge_slots.tomorrow.start_hour
        if ems.charge_slots.tomorrow
        else None,
    ),
    SmartRceSensorDescription(
        name="Start Charge Hour Tomorrow Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.charge_slots.tomorrow.start_datetime
        if ems.charge_slots.tomorrow
        else None,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Tomorrow",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.charge_slots.tomorrow.end_hour
        if ems.charge_slots.tomorrow
        else None,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Tomorrow Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.charge_slots.tomorrow.end_datetime
        if ems.charge_slots.tomorrow
        else None,
    ),
)
