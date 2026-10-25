"""Where a month's deposit came from: the PV, or the battery selling in the evening.

The deposit is earned hour by hour (`value_day`), and a single hour's export can
be sun that the house did not need or charge the battery gave up in a dear hour.
The meter cannot tell them apart; the battery's discharge counter can, under one
convention: the PV feeds the house first and the battery covers what the PV
leaves short, so whatever the battery gave beyond that is what left the house.
Battery export of an hour is therefore min(export, battery discharge). In the
evening the PV is zero and this is exact; in a morning sale with weak sun it is a
close approximation.

The battery's share includes energy it took from the grid (the night charge to
the T1 reserve, the afternoon top-up) — the counter does not know the source.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from .settlement_regime import deposit_coefficient

if TYPE_CHECKING:
    from collections.abc import Mapping
    import datetime

    from .billing_month import BillingMonth
    from .hourly_history import PricedHour

_PLN_PER_MWH: Final = 1000.0


@dataclass(frozen=True)
class ExportOrigin:
    """One month's export and the deposit it earned, split by source."""

    month: BillingMonth
    pv_kwh: float
    battery_kwh: float
    pv_pln: float
    battery_pln: float

    @classmethod
    def of_month(
        cls,
        month: BillingMonth,
        hours: Mapping[datetime.date, Mapping[int, PricedHour]],
        discharge: Mapping[tuple[datetime.date, int], float],
    ) -> ExportOrigin:
        """Split the metered hours of `month`; hours without a price earn nothing."""
        coefficient = deposit_coefficient(month)
        pv_kwh = battery_kwh = pv_pln = battery_pln = 0.0
        for day, by_hour in hours.items():
            for hour, metered in by_hour.items():
                exported = max(0.0, metered.exported_kwh)
                from_battery = min(exported, max(0.0, discharge.get((day, hour), 0.0)))
                pv_kwh += exported - from_battery
                battery_kwh += from_battery
                if metered.price_pln_mwh is None:
                    continue
                pln_per_kwh = (
                    max(0.0, metered.price_pln_mwh) / _PLN_PER_MWH * coefficient
                )
                pv_pln += (exported - from_battery) * pln_per_kwh
                battery_pln += from_battery * pln_per_kwh
        return cls(
            month=month,
            pv_kwh=pv_kwh,
            battery_kwh=battery_kwh,
            pv_pln=pv_pln,
            battery_pln=battery_pln,
        )
