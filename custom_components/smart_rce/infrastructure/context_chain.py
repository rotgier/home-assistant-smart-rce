"""HA Context chaining helper for smart_rce actuators.

Mirrors the `EVENT_AUTOMATION_TRIGGERED` pattern from
`homeassistant.components.automation`: fire a "smart_rce_action" event
with phase/reason metadata, then return a child Context with
`parent_id=event.context.id` for the downstream `scene.apply` call.

HA logbook describer (`custom_components/smart_rce/logbook.py`) renders
the event as "triggered by Smart RCE phase=X", and the resulting
state_changed entry is linked via parent_id chain — producing
"DoD changed to 90 triggered by Smart RCE phase=X (reason=...)".
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import Context, HomeAssistant

from ..const import ATTR_PHASE, ATTR_REASON, EVENT_SMART_RCE_ACTION


def fire_action_and_chain_context(
    hass: HomeAssistant,
    user_id: str,
    *,
    phase: str,
    reason: str | None = None,
) -> Context:
    """Fire smart_rce_action + return chained Context for downstream service calls.

    Returns a Context whose `parent_id` is the fired event's context id —
    pass it as `context=` to `hass.services.async_call(...)` so HA logbook
    can walk the parent chain when rendering the resulting state change.
    """
    trigger_ctx = Context(user_id=user_id)
    data: dict[str, Any] = {ATTR_PHASE: phase}
    if reason:
        data[ATTR_REASON] = reason
    hass.bus.async_fire(EVENT_SMART_RCE_ACTION, data, context=trigger_ctx)
    return Context(parent_id=trigger_ctx.id, user_id=user_id)
