"""SavingsService — recomputes self-consumption volumes from the recorder.

Deliberately independent of the meter refresh: this source is local, free and
never purged, so it recomputes the whole measured era every run instead of
tracking a watermark. Nothing here talks to TAURON, so it can run as often as
convenient and cannot contribute to a ban.
"""

from __future__ import annotations

from collections import defaultdict
import datetime
import logging
from typing import TYPE_CHECKING, Final

from ..domain.billing_month import BillingMonth
from ..domain.self_consumption import self_consumption_by_zone
from ..domain.tariff import Zone

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..domain.self_consumption import HouseholdHour
    from ..infrastructure.household_energy_reader import HouseholdEnergyReader
    from .deposit_service import DepositService

# Hourly household statistics begin late in September 2024, which is also when
# the G13 zones took effect. Everything before that is covered by the seed's
# legacy figure, so measuring starts at the first month that is both complete
# and zoned.
FIRST_MEASURED_MONTH: Final = BillingMonth(2024, 10)

_LOGGER = logging.getLogger(__name__)


class SavingsService:
    """Keeps the deposit report's self-consumption volumes up to date."""

    def __init__(self, reader: HouseholdEnergyReader, deposit: DepositService) -> None:
        self._reader = reader
        self._deposit = deposit

    async def async_refresh(self, today: datetime.date) -> None:
        """Recompute monthly zonal self-consumption and hand it to the report."""
        start = datetime.date(FIRST_MEASURED_MONTH.year, FIRST_MEASURED_MONTH.month, 1)
        if today <= start:
            return
        days = await self._reader.async_hours_between(start, today)
        self._deposit.update_self_consumption(_aggregate(days))
        _LOGGER.debug("SavingsService: recomputed from %d days", len(days))


def _aggregate(
    days: Mapping[datetime.date, Mapping[int, HouseholdHour]],
) -> dict[BillingMonth, dict[Zone, float]]:
    """Sum daily zonal self-consumption into months."""
    per_month: dict[BillingMonth, dict[Zone, float]] = defaultdict(
        lambda: dict.fromkeys(Zone, 0.0)
    )
    for day, hours in days.items():
        month = BillingMonth(day.year, day.month)
        for zone, kwh in self_consumption_by_zone(day, hours).items():
            per_month[month][zone] += kwh
    return dict(per_month)
