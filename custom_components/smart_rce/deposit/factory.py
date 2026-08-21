"""Deposit composition root — wires the deposit bounded context (ADR-025).

The context is designed to degrade rather than fail. Without TAURON credentials
it still reports everything derived from the stored history (seeded from the
invoice-reconciled calculator), just frozen in time; the daily refresh is what
credentials unlock. And the refresh itself is wrapped so that a scraper outage
logs and moves on instead of taking the integration down with it.
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
from .application.refresh_service import DepositRefreshService
from .infrastructure.elicznik_reader import ElicznikReader
from .infrastructure.history_repository import HistoryRepository
from .infrastructure.rce_price_reader import PriceSource, RcePriceReader
from .infrastructure.report_writer import async_write_debug_report
from .infrastructure.resources import load_tariff

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
    refresh: DepositRefreshService | None


async def create_deposit(
    hass: HomeAssistant, entry: SmartRceConfigEntry, prices: PriceSource
) -> Deposit:
    """Wire the deposit context (call from async_setup_entry before runtime_data)."""
    tasks = AsyncTaskRunner(hass, entry)
    repository = HistoryRepository(hass, tasks)
    await repository.async_restore()
    tariff = await hass.async_add_executor_job(load_tariff)
    service = DepositService(tariff, repository.history)
    websocket_api.async_register(hass)

    refresh = _build_refresh(hass, entry, repository, prices, service)
    await _publish(hass, service)
    return Deposit(service=service, refresh=refresh)


def _build_refresh(
    hass: HomeAssistant,
    entry: SmartRceConfigEntry,
    repository: HistoryRepository,
    prices: PriceSource,
    service: DepositService,
) -> DepositRefreshService | None:
    """Schedule the daily refresh, or return None when credentials are missing."""
    username = entry.options.get(CONF_USERNAME)
    password = entry.options.get(CONF_PASSWORD)
    if not username or not password:
        _LOGGER.info(
            "Deposit: no TAURON credentials in options — reporting from stored "
            "history only (last data day %s)",
            repository.history.last_data_day,
        )
        return None

    refresh = DepositRefreshService(
        repository,
        RcePriceReader(prices),
        ElicznikReader(hass, username, password),
        on_updated=service.recalculate,
    )

    async def _run(_now: datetime.datetime | None = None) -> None:
        try:
            if await refresh.async_refresh(dt_util.now().date()):
                await _publish(hass, service)
        except Exception:  # noqa: BLE001 - a scraper outage must not break setup
            _LOGGER.exception("Deposit refresh failed; will retry on the next run")

    entry.async_on_unload(
        async_track_time_change(
            hass, _run, hour=_REFRESH_HOUR, minute=_REFRESH_MINUTE, second=0
        )
    )
    # Catch up at startup too: after a restart the watermark is usually a day or
    # more behind, and waiting until 04:15 would leave the report visibly stale.
    entry.async_create_background_task(hass, _run(), "smart_rce_deposit_first_refresh")
    return refresh


async def _publish(hass: HomeAssistant, service: DepositService) -> None:
    await async_write_debug_report(hass, service.report.to_dict())
