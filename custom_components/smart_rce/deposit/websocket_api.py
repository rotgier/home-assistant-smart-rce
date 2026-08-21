"""WebSocket command exposing the deposit report to the dashboard.

Driving adapter. The month-by-month tables travel this way rather than as sensor
attributes: the recorder rewrites the full attribute set on every state change,
so a thirty-row table would grow the database for no benefit (ADR-025 #7). The
scalars worth graphing stay as sensors.

Read-only, so no admin requirement — any authenticated frontend session may ask.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from ..const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.components.websocket_api import ActiveConnection

REPORT_COMMAND: Final = "smart_rce/deposit/report"


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register the deposit WebSocket commands."""
    websocket_api.async_register_command(hass, _handle_report)


@websocket_api.websocket_command({vol.Required("type"): REPORT_COMMAND})
@callback
def _handle_report(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the current deposit report as a plain dict."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries or not hasattr(entries[0], "runtime_data"):
        connection.send_error(msg["id"], "not_loaded", "smart_rce is not loaded")
        return
    connection.send_result(msg["id"], entries[0].runtime_data.deposit.report.to_dict())
