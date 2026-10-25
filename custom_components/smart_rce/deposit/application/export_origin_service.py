"""ExportOriginService — splits the deposit of recent months into PV and battery.

Runs with the daily refresh, after the meter has stored the latest hours. Reads
the current and the previous month: the previous one keeps changing for a week
while TAURON fills its last days in. Local sources only — the hour archive and
the recorder — so it can run as often as convenient.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

from ..domain.billing_month import BillingMonth
from ..domain.export_origin import ExportOrigin

if TYPE_CHECKING:
    from ..infrastructure.battery_discharge_reader import BatteryDischargeReader
    from ..infrastructure.hour_archive import HourArchive
    from .deposit_service import DepositService

_LOGGER = logging.getLogger(__name__)


class ExportOriginService:
    """Keeps the deposit report's PV / battery split up to date."""

    def __init__(
        self,
        archive: HourArchive,
        reader: BatteryDischargeReader,
        deposit: DepositService,
    ) -> None:
        self._archive = archive
        self._reader = reader
        self._deposit = deposit

    async def async_refresh(self, today: datetime.date) -> None:
        """Recompute the previous and the current month and hand them over."""
        current = BillingMonth(today.year, today.month)
        by_month: dict[BillingMonth, ExportOrigin] = {}
        for month in (current.previous(), current):
            first = datetime.date(month.year, month.month, 1)
            last = first + datetime.timedelta(days=month.days - 1)
            hours = (await self._archive.async_month(month)).days
            discharge = await self._reader.async_hours_between(first, last)
            by_month[month] = ExportOrigin.of_month(month, hours, discharge)
        self._deposit.update_export_origin(by_month)
        _LOGGER.debug("ExportOriginService: %s", by_month)
