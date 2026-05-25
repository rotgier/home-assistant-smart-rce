"""Logbook describer for smart_rce actions.

Mirrors `homeassistant.components.automation.logbook` pattern.

When an actuator (DodPolicyActuator / GoodweEmsActuator) is about to apply
state to the inverter, it fires `EVENT_SMART_RCE_ACTION` with phase + reason
metadata, then performs `scene.apply` with a child `Context(parent_id=...)`.

HA logbook's processor walks the `parent_id` chain when rendering a
state_changed entry and merges contributing describer outputs. With this
describer registered, an inverter state change reads as:

    "GoodWe DoD changed to 90 triggered by Smart RCE phase=INTERVENTIONS_BLOCKED"

— analog to the existing "triggered by automation X triggered by time"
cascades produced by HA's own automation describer.

Auto-discovered by `homeassistant.components.logbook` via
`async_process_integration_platforms` (no manifest dependency needed).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_CONTEXT_ID,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
    LOGBOOK_ENTRY_SOURCE,
    LazyEventPartialState,
)
from homeassistant.core import HomeAssistant, callback

from .const import ATTR_PHASE, ATTR_REASON, DOMAIN, EVENT_SMART_RCE_ACTION


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[
        [str, str, Callable[[LazyEventPartialState], dict[str, Any]]], None
    ],
) -> None:
    """Describe logbook events for smart_rce."""

    @callback
    def describe(event: LazyEventPartialState) -> dict[str, Any]:
        data = event.data
        phase = data.get(ATTR_PHASE) or "?"
        reason = data.get(ATTR_REASON)
        message = f"phase={phase}"
        if reason:
            message = f"{message} ({reason})"
        return {
            LOGBOOK_ENTRY_NAME: "Smart RCE",
            LOGBOOK_ENTRY_MESSAGE: message,
            LOGBOOK_ENTRY_SOURCE: phase,
            LOGBOOK_ENTRY_CONTEXT_ID: event.context_id,
        }

    async_describe_event(DOMAIN, EVENT_SMART_RCE_ACTION, describe)
