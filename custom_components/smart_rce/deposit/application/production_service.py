"""ProductionService — keeps measured PV production in the report up to date.

Same shape as `SavingsService` and for the same reason: the recorder is local and
never purged, so the whole measured era is recomputed every run instead of
tracking a watermark that could go subtly wrong.

Production explains nothing about the deposit on its own — it is here so the
dashboard can answer "did the deposit change because the sun changed?" without
the reader having to trust a separate source.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Final

from ..domain.billing_month import BillingMonth

if TYPE_CHECKING:
    from ..infrastructure.pv_production_reader import PvProductionReader
    from .deposit_service import DepositService

# The production sensor starts late in September 2024; that month is therefore
# partial and would read as a slump. Measuring starts at the first whole month,
# and everything earlier comes from the seed.
FIRST_MEASURED_MONTH: Final = BillingMonth(2024, 10)

_LOGGER = logging.getLogger(__name__)


class ProductionService:
    """Feeds monthly PV generation into the deposit report."""

    def __init__(self, reader: PvProductionReader, deposit: DepositService) -> None:
        self._reader = reader
        self._deposit = deposit

    async def async_refresh(self, today: datetime.date) -> None:
        """Recompute monthly production and hand it to the report."""
        start = datetime.date(FIRST_MEASURED_MONTH.year, FIRST_MEASURED_MONTH.month, 1)
        if today <= start:
            return
        months = await self._reader.async_months_between(start, today)
        self._deposit.update_production(months)
        _LOGGER.debug("ProductionService: %d months measured", len(months))
