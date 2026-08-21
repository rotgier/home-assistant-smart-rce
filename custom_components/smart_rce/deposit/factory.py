"""Deposit composition root — wires the deposit bounded context (ADR-025).

Phase 1 has no live inputs: the shipped resources are read once (in an executor,
they are blocking file reads) and the service derives everything from them. The
cross-context port for hourly RCE prices arrives with phase 2; until then the
context has no dependency on `ems` at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import websocket_api
from .application.deposit_service import DepositService
from .infrastructure.report_writer import async_write_debug_report
from .infrastructure.resources import load_seed_history, load_tariff

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def create_deposit(hass: HomeAssistant) -> DepositService:
    """Build the deposit context."""
    tariff = await hass.async_add_executor_job(load_tariff)
    seed = await hass.async_add_executor_job(load_seed_history)
    service = DepositService(tariff, seed)
    websocket_api.async_register(hass)
    await async_write_debug_report(hass, service.report.to_dict())
    return service
