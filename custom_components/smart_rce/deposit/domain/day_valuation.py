"""Turning a metered day into a settled DayRecord.

Two rules meet here, both of which the bill applies hour by hour: exported energy
is worth `RCE[h] x coefficient`, and imported energy lands in the tariff zone of
the hour it arrived in. Doing it per hour is not an optimisation — a daily average
would misprice both sides, because export concentrates in the evening peak and
import in the cheap night.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Final

from .billing_month import BillingMonth
from .settlement_history import DayRecord
from .settlement_regime import deposit_coefficient
from .tariff import Zone
from .tariff_zones import zone_for

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .meter_reading import HourReading

_PLN_PER_MWH: Final = 1000.0


def value_day(
    day: datetime.date,
    readings: Mapping[int, HourReading],
    prices: Mapping[int, float],
) -> DayRecord:
    """Value one metered day. Hours without a price contribute no deposit."""
    coefficient = deposit_coefficient(BillingMonth(day.year, day.month))
    exported = 0.0
    earned = 0.0
    imported: dict[Zone, float] = dict.fromkeys(Zone, 0.0)
    for hour, reading in readings.items():
        exported += reading.exported_kwh
        price = prices.get(hour)
        if price is not None:
            earned += reading.exported_kwh * price / _PLN_PER_MWH * coefficient
        zone = zone_for(datetime.datetime.combine(day, datetime.time(hour=hour)))
        imported[zone] += reading.imported_kwh
    return DayRecord(
        day=day,
        exported_kwh=exported,
        deposit_earned=earned,
        import_kwh=imported,
    )
