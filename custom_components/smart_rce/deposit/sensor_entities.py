"""Deposit sensors — the scalars worth graphing and alerting on.

Only scalars live here. The month-by-month tables go out through the WebSocket
command instead: the recorder rewrites every attribute on each state change, and
a 30-row table would bloat the database for no benefit (ADR-025 #7).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import datetime
from typing import TYPE_CHECKING, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.helpers.typing import StateType

from .application.report import DepositReport
from .const import DEPOSIT_UNIQUE_ID_PREFIX
from .deposit_device import deposit_device_info

if TYPE_CHECKING:
    from custom_components.smart_rce import SmartRceConfigEntry

_PLN: Final = "PLN"


def build_sensors(entry: SmartRceConfigEntry) -> list[SensorEntity]:
    """Deposit sensor entities for the top-level `sensor` platform to add."""
    return [DepositSensor(entry, description) for description in SENSOR_DESCRIPTIONS]


class DepositSensor(SensorEntity):
    """Reads one scalar off the current `DepositReport`."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: DepositSensorDescription

    def __init__(
        self, entry: SmartRceConfigEntry, description: DepositSensorDescription
    ) -> None:
        self._entry = entry
        self.entity_description = description
        self._attr_device_info = deposit_device_info(entry)
        self._attr_unique_id = f"{DEPOSIT_UNIQUE_ID_PREFIX}_{description.key}"

    async def async_added_to_hass(self) -> None:
        """Re-render whenever the daily refresh rebuilds the report."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._entry.runtime_data.deposit.service.add_listener(
                self.async_write_ha_state
            )
        )

    @property
    def native_value(self) -> StateType | datetime.date:
        return self.entity_description.value_fn(
            self._entry.runtime_data.deposit.service.report
        )


@dataclass(frozen=True, kw_only=True)
class DepositSensorDescription(SensorEntityDescription):
    """Description schema — `value_fn` pulls a scalar out of the report.

    `key` is given explicitly rather than derived from `name` (the idiom used by
    `EmsSensorDescription`): it feeds `unique_id`, so deriving it would turn every
    label change into a new entity plus an orphan with the history attached to it
    (ADR-015).
    """

    value_fn: Callable[[DepositReport], StateType | datetime.date]


def _rounded(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


SENSOR_DESCRIPTIONS: tuple[DepositSensorDescription, ...] = (
    DepositSensorDescription(
        key="balance",
        name="Balance",
        native_unit_of_measurement=_PLN,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda report: _rounded(report.balance),
        icon="mdi:cash-multiple",
    ),
    DepositSensorDescription(
        # What the settled balance does not show: the month in progress. The
        # settled figure is the one that reconciles with the invoice, this is the
        # one that answers "how much do I have right now".
        key="balance_running",
        name="Balance Running",
        native_unit_of_measurement=_PLN,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda report: _rounded(report.balance_running),
        icon="mdi:cash-clock",
    ),
    DepositSensorDescription(
        # Freshness gauge: if this stops advancing, the daily fetch has stopped
        # and every other number here is quietly frozen with it.
        key="last_data_day",
        name="Last Data Day",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda report: report.last_data_day,
        icon="mdi:calendar-check",
    ),
    DepositSensorDescription(
        key="capacity",
        name="Capacity",
        native_unit_of_measurement=_PLN,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda report: _rounded(report.capacity.value),
        icon="mdi:gauge-full",
    ),
    DepositSensorDescription(
        key="utilization",
        name="Utilization",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda report: _rounded(report.utilization, 1),
        icon="mdi:percent",
    ),
    DepositSensorDescription(
        # The strategy trigger: at 100% the yearly peak outgrows what a year of
        # bills can absorb, and tranches start expiring at a partial refund.
        key="peak_utilization",
        name="Peak Utilization",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda report: _rounded(report.peak_utilization, 1),
        icon="mdi:chart-bell-curve",
    ),
    DepositSensorDescription(
        key="oldest_tranche_age",
        name="Oldest Tranche Age",
        native_unit_of_measurement="mies.",
        value_fn=lambda report: report.oldest_tranche_age,
        icon="mdi:timer-sand",
    ),
    DepositSensorDescription(
        key="first_forfeit_year",
        name="First Forfeit Year",
        value_fn=lambda report: report.first_forfeit_year,
        icon="mdi:calendar-alert",
    ),
    DepositSensorDescription(
        # What the installation has saved since it was switched on: energy never
        # bought plus deposit actually spent on the bill.
        key="savings_total",
        name="Savings Total",
        native_unit_of_measurement=_PLN,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda report: _rounded(report.savings.total_pln),
        icon="mdi:piggy-bank",
    ),
    DepositSensorDescription(
        key="savings_self_consumption",
        name="Savings Self Consumption",
        native_unit_of_measurement=_PLN,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda report: _rounded(report.savings.self_consumption_pln),
        icon="mdi:home-lightning-bolt",
    ),
    DepositSensorDescription(
        key="savings_deposit",
        name="Savings Deposit",
        native_unit_of_measurement=_PLN,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda report: _rounded(report.savings.deposit_pln),
        icon="mdi:cash-refund",
    ),
    DepositSensorDescription(
        # Gross, to line up with `input_number.rce_high_price_threshold_gross`
        # and the RCE sensors — the whole system quotes RCE x 1.23.
        key="break_even_rce",
        name="Break Even RCE",
        native_unit_of_measurement="PLN/MWh",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda report: _rounded(report.break_even_rce_gross, 0),
        icon="mdi:scale-balance",
    ),
    DepositSensorDescription(
        key="winter_trough",
        name="Winter Trough",
        native_unit_of_measurement=_PLN,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda report: _rounded(report.winter.trough.settlement.balance),
        icon="mdi:snowflake-alert",
    ),
)
