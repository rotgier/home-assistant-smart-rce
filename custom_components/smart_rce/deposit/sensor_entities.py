"""Deposit sensors — the scalars worth graphing and alerting on.

Only scalars live here. The month-by-month tables go out through the WebSocket
command instead: the recorder rewrites every attribute on each state change, and
a 30-row table would bloat the database for no benefit (ADR-025 #7).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)

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

    @property
    def native_value(self) -> str | int | float | None:
        return self.entity_description.value_fn(self._entry.runtime_data.deposit.report)


@dataclass(frozen=True, kw_only=True)
class DepositSensorDescription(SensorEntityDescription):
    """Description schema — `value_fn` pulls a scalar out of the report.

    `key` is given explicitly rather than derived from `name` (the idiom used by
    `EmsSensorDescription`): it feeds `unique_id`, so deriving it would turn every
    label change into a new entity plus an orphan with the history attached to it
    (ADR-015).
    """

    value_fn: Callable[[DepositReport], str | int | float | None]


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
        key="break_even_rce",
        name="Break Even RCE",
        native_unit_of_measurement="PLN/MWh",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda report: _rounded(report.break_even_rce, 0),
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
