"""G13 tariff — zones, rates, and what an imported kWh costs.

Shared deliberately: the tariff belongs to neither bounded context. Settlement
needs it to price what the meter recorded; the evening planner needs it to know
whether exporting a stored kWh beats keeping it for the house. Both read the
same URE decision, so both read the same table.

Rates live in `tariff_table.json`, one row per month, net PLN/kWh with energy
and distribution kept apart — the deposit offsets energy only, distribution is
always cash. Adding a new tariff means adding a row there and nothing else.
"""

from .rates import VAT, Zone, ZoneRates
from .zones import evening_peak, is_holiday, zone_for

__all__ = [
    "VAT",
    "Zone",
    "ZoneRates",
    "evening_peak",
    "is_holiday",
    "zone_for",
]
