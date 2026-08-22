"""Deposit composition root — wires the deposit bounded context (ADR-025).

The context is designed to degrade rather than fail. Without TAURON credentials
it still reports everything derived from the stored history (seeded from the
invoice-reconciled calculator) and still measures self-consumption, which comes
from the recorder; only the settlement fetch goes quiet. Every scheduled job is
wrapped so an outage logs and moves on instead of taking the integration down.

Two sources, deliberately independent:
- the meter (TAURON eLicznik) — remote, rate-limited, needs credentials;
- household energy (HA recorder) — local, free, never purged.
Coupling them would let a TAURON outage block savings that are computable
offline, so the daily job runs each on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import logging
from typing import TYPE_CHECKING

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from ..infrastructure.async_task_runner import AsyncTaskRunner
from . import websocket_api
from .application.deposit_service import DepositService
from .application.market_price_service import MarketPriceService
from .application.production_service import ProductionService
from .application.refresh_service import DepositRefreshService
from .application.savings_service import SavingsService
from .infrastructure.elicznik_reader import ElicznikReader
from .infrastructure.history_repository import HistoryRepository
from .infrastructure.household_energy_reader import HouseholdEnergyReader
from .infrastructure.pv_production_reader import PvProductionReader
from .infrastructure.rce_price_reader import PriceSource, RcePriceReader
from .infrastructure.rcem_reader import PseRcemReader
from .infrastructure.report_writer import async_write_debug_report
from .infrastructure.resources import (
    load_monthly_prices,
    load_seed_history,
    load_tariff,
)

if TYPE_CHECKING:
    from custom_components.smart_rce import SmartRceConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Early morning: the previous day is closed at the meter and PSE has published
# its prices, while nothing else is competing for the network.
_REFRESH_HOUR = 4
_REFRESH_MINUTE = 15


@dataclass
class Deposit:
    """Deposit bounded context — public services exposed to platforms."""

    service: DepositService
    savings: SavingsService
    production: ProductionService
    market_prices: MarketPriceService
    refresh: DepositRefreshService | None


async def create_deposit(
    hass: HomeAssistant, entry: SmartRceConfigEntry, prices: PriceSource
) -> Deposit:
    """Wire the deposit context (call from async_setup_entry before runtime_data)."""
    repository = HistoryRepository(hass, AsyncTaskRunner(hass, entry))
    await repository.async_restore()
    tariff = await hass.async_add_executor_job(load_tariff)
    monthly_prices = await hass.async_add_executor_job(load_monthly_prices)
    seed = await hass.async_add_executor_job(load_seed_history)
    service = DepositService(
        tariff,
        repository.history,
        legacy=seed.legacy_months,
        monthly_prices=monthly_prices,
        seed_production=seed.production,
    )
    websocket_api.async_register(hass)

    savings = SavingsService(HouseholdEnergyReader(hass), service)
    production = ProductionService(PvProductionReader(hass), service)
    market_prices = MarketPriceService(PseRcemReader(hass), service)
    refresh = _build_refresh(hass, entry, repository, prices)
    _schedule_daily(hass, entry, service, savings, production, market_prices, refresh)
    await _publish(hass, service)
    return Deposit(
        service=service,
        savings=savings,
        production=production,
        market_prices=market_prices,
        refresh=refresh,
    )


def _build_refresh(
    hass: HomeAssistant,
    entry: SmartRceConfigEntry,
    repository: HistoryRepository,
    prices: PriceSource,
) -> DepositRefreshService | None:
    """Build the meter refresh, or None when credentials are missing."""
    username = entry.options.get(CONF_USERNAME)
    password = entry.options.get(CONF_PASSWORD)
    if not username or not password:
        _LOGGER.info(
            "Deposit: no TAURON credentials in options — reporting from stored "
            "history only (last data day %s)",
            repository.history.last_data_day,
        )
        return None
    return DepositRefreshService(
        repository,
        RcePriceReader(prices),
        ElicznikReader(hass, username, password),
        on_updated=lambda: None,  # the daily job republishes once both sources ran
    )


def _schedule_daily(
    hass: HomeAssistant,
    entry: SmartRceConfigEntry,
    service: DepositService,
    savings: SavingsService,
    production: ProductionService,
    market_prices: MarketPriceService,
    refresh: DepositRefreshService | None,
) -> None:
    """Run every source once a day, and once now to catch up after a restart."""

    async def _run(_now: datetime.datetime | None = None) -> None:
        today = dt_util.now().date()
        if refresh is not None:
            try:
                await refresh.async_refresh(today)
            except Exception:  # noqa: BLE001 - a scraper outage must not spread
                _LOGGER.exception("Deposit: meter refresh failed, retrying next run")
        try:
            await savings.async_refresh(today)
        except Exception:  # noqa: BLE001 - reporting extra, never fatal
            _LOGGER.exception("Deposit: self-consumption refresh failed")
        try:
            await production.async_refresh(today)
        except Exception:  # noqa: BLE001 - reporting extra, never fatal
            _LOGGER.exception("Deposit: production refresh failed")
        try:
            await market_prices.async_refresh()
        except Exception as err:  # noqa: BLE001 - a scraped page, expected to rot
            _LOGGER.debug("Deposit: RCEm refresh failed (%s), using shipped table", err)
        service.recalculate()
        await _publish(hass, service)

    entry.async_on_unload(
        async_track_time_change(
            hass, _run, hour=_REFRESH_HOUR, minute=_REFRESH_MINUTE, second=0
        )
    )
    # After a restart the watermark is usually a day or more behind, and waiting
    # until 04:15 would leave a visibly stale report on the dashboard.
    entry.async_create_background_task(hass, _run(), "smart_rce_deposit_refresh")


async def _publish(hass: HomeAssistant, service: DepositService) -> None:
    await async_write_debug_report(hass, service.report.to_dict())
