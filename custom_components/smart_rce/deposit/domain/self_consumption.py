"""Self-consumption — the energy the installation kept out of the grid.

Measured as *avoided import* rather than as instantaneous PV self-use: for each
hour, whatever the household consumed beyond what it had to buy. That includes
battery discharge, which is the point — a kWh that came out of the battery is a
kWh not bought, and it is priced at the zone it displaced.

Working on the hourly-balanced net is what makes it match the invoice: export and
import inside one hour cancel there too.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
from typing import TYPE_CHECKING

from .tariff import Zone
from .tariff_zones import zone_for

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class HouseholdHour:
    """One hour as the house saw it."""

    consumption_kwh: float
    net_kwh: float
    """Hourly-balanced grid net: positive is export, negative is import."""

    @property
    def imported_kwh(self) -> float:
        return max(0.0, -self.net_kwh)

    @property
    def self_consumed_kwh(self) -> float:
        """Consumption that never came from the grid — PV direct plus battery."""
        return max(0.0, self.consumption_kwh - self.imported_kwh)


def self_consumption_by_zone(
    day: datetime.date, hours: Mapping[int, HouseholdHour]
) -> dict[Zone, float]:
    """Split a day's self-consumption across the zones it displaced."""
    per_zone: dict[Zone, float] = dict.fromkeys(Zone, 0.0)
    for hour, reading in hours.items():
        zone = zone_for(datetime.datetime.combine(day, datetime.time(hour=hour)))
        per_zone[zone] += reading.self_consumed_kwh
    return per_zone
