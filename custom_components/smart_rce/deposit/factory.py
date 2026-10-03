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
from .application.reference_days_service import ReferenceDaysService
from .application.refresh_service import DepositRefreshService
from .application.savings_service import SavingsService
from .infrastructure.elicznik_reader import ElicznikReader
from .infrastructure.history_repository import HistoryRepository
from .infrastructure.hour_archive import HourArchive
from .infrastructure.household_energy_reader import HouseholdEnergyReader
from .infrastructure.market_price_repository import MarketPriceRepository
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

# Midday, not the early morning it used to be: measured on 2026-08-27, TAURON had
# nothing for the previous day at 04:15 and a complete day by the evening. It
# publishes at an hour it does not state, so there is a second attempt in the
# afternoon — the service ignores it when yesterday is already in.
_REFRESH_HOURS = (12, 17)
_REFRESH_MINUTE = 15


@dataclass
class Deposit:
    """Deposit bounded context — public services exposed to platforms."""

    service: DepositService
    savings: SavingsService
    production: ProductionService
    market_prices: MarketPriceService
    reference_days: ReferenceDaysService
    refresh: DepositRefreshService | None


async def create_deposit(
    hass: HomeAssistant, entry: SmartRceConfigEntry, prices: PriceSource
) -> Deposit:
    """Wire the deposit context (call from async_setup_entry before runtime_data)."""
    tasks = AsyncTaskRunner(hass, entry)
    repository = HistoryRepository(hass, tasks)
    await repository.async_restore()
    prices_repository = MarketPriceRepository(hass, tasks)
    await prices_repository.async_restore()
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
    # Everything scraped since the release was cut, layered over the shipped table.
    service.update_market_prices(prices_repository.prices.by_month)
    websocket_api.async_register(hass)

    savings = SavingsService(HouseholdEnergyReader(hass), service)
    production = ProductionService(PvProductionReader(hass), service)
    archive = HourArchive(hass)
    deposit = Deposit(
        service=service,
        savings=savings,
        production=production,
        reference_days=ReferenceDaysService(archive, service),
        market_prices=MarketPriceService(
            PseRcemReader(hass), service, prices_repository
        ),
        refresh=_build_refresh(hass, entry, repository, prices, archive),
    )
    # Before anything reads the report: without last year's days the current month
    # is only the days measured so far, which makes it look like a month of barely
    # using anything — the projection then reports a trough that is too high and a
    # capacity that is too low. The daily job would fix it a second later, but a
    # failure there would leave those numbers standing, quietly wrong.
    await _load_reference_days(deposit)
    _schedule_daily(hass, entry, deposit)
    await _publish(hass, service)
    return deposit


async def _load_reference_days(deposit: Deposit) -> None:
    """Fill the rest of the current month before the first report is built."""
    try:
        await deposit.reference_days.async_refresh(dt_util.now().date())
    except Exception:  # noqa: BLE001 - projection detail, never blocks setup
        _LOGGER.exception("Deposit: could not load last year's days at startup")


def _build_refresh(
    hass: HomeAssistant,
    entry: SmartRceConfigEntry,
    repository: HistoryRepository,
    prices: PriceSource,
    archive: HourArchive,
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
        archive,
        on_updated=lambda: None,  # the daily job republishes once both sources ran
    )


def _schedule_daily(
    hass: HomeAssistant, entry: SmartRceConfigEntry, deposit: Deposit
) -> None:
    """Run every source once a day, and once now to catch up after a restart.

    Takes the whole context rather than its five services one by one: they are
    exactly the fields of `Deposit`, and passing them separately is how the
    signature grew to seven arguments as sources were added.
    """

    async def _run(_now: datetime.datetime | None = None) -> None:
        now = dt_util.now()
        today = now.date()
        if deposit.refresh is not None:
            try:
                await deposit.refresh.async_refresh(now)
            except Exception:  # noqa: BLE001 - a scraper outage must not spread
                _LOGGER.exception("Deposit: meter refresh failed, retrying next run")
        try:
            await deposit.savings.async_refresh(today)
        except Exception:  # noqa: BLE001 - reporting extra, never fatal
            _LOGGER.exception("Deposit: self-consumption refresh failed")
        try:
            await deposit.production.async_refresh(today)
        except Exception:  # noqa: BLE001 - reporting extra, never fatal
            _LOGGER.exception("Deposit: production refresh failed")
        try:
            await deposit.reference_days.async_refresh(today)
        except Exception:  # noqa: BLE001 - projection detail, never fatal
            _LOGGER.exception("Deposit: reference days refresh failed")
        try:
            await deposit.market_prices.async_refresh()
        except Exception as err:  # noqa: BLE001 - a scraped page, expected to rot
            _LOGGER.debug("Deposit: RCEm refresh failed (%s), using shipped table", err)
        deposit.service.recalculate()
        await _publish(hass, deposit.service)

    entry.async_on_unload(
        async_track_time_change(
            hass, _run, hour=_REFRESH_HOURS, minute=_REFRESH_MINUTE, second=0
        )
    )
    # After a restart the watermark is usually a day or more behind, and waiting
    # until midday would leave a visibly stale report on the dashboard.
    entry.async_create_background_task(hass, _run(), "smart_rce_deposit_refresh")


async def _publish(hass: HomeAssistant, service: DepositService) -> None:
    await async_write_debug_report(hass, service.report.to_dict())
