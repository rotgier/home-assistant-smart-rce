"""HA Context helper for smart_rce actuators — logbook attribution pattern.

Mirrors the `EVENT_AUTOMATION_TRIGGERED` pattern from
`homeassistant.components.automation`: fire a "smart_rce_action" event
with phase/reason metadata, then reuse **the same** Context for the
downstream `scene.apply` / `number.set_value` call.

Why same Context (not child `Context(parent_id=...)`):
HA logbook's `ContextAugmenter.augment()` does NOT walk `parent_id`
chain when resolving describer for a state_changed row. It calls
`get_context(state.context_id)` which returns the FIRST row that wrote
to `context_lookup[ctx_id]` (first-write-wins, chronological order —
`logbook/processor.py:307`). If we use `Context(parent_id=...)` then
CALL_SERVICE event has a NEW context_id (B), nothing else wrote B
earlier → describer never reached, logbook shows "triggered by action
Number: Set" (default CALL_SERVICE attribution).

Correct pattern: fire smart_rce_action with context A, pass SAME A to
service.async_call. Both events share ctx=A. context_lookup[A] gets
smart_rce_action (chronologically first, since fire is synchronous and
happens before await services.async_call). State_changed.context_id=A
→ get_context(A) → smart_rce_action row → describer renders
"Smart RCE phase=X (reason=...)".

Verified against 22.05 logbook example where automation chain
"triggered by automation X triggered by time" worked — automation
fires EVENT_AUTOMATION_TRIGGERED with trigger_context, then uses
trigger_context (NOT child) for action service calls.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import Context, HomeAssistant

from ..const import ATTR_PHASE, ATTR_REASON, EVENT_SMART_RCE_ACTION


def fire_action_event_for_ctx(
    hass: HomeAssistant,
    *,
    phase: str,
    reason: str | None = None,
    parent_context: Context | None = None,
) -> Context:
    """Fire smart_rce_action event + return the SAME Context for service call.

    Pass the returned Context as `context=` to
    `hass.services.async_call(...)`. Both the fired event and the
    subsequent CALL_SERVICE / state_changed share the same context_id,
    so HA logbook's context_lookup resolves to the smart_rce_action row
    (first writer chronologically) and our describer renders the
    "Smart RCE phase=X" attribution.

    See module docstring for the why-not-child-Context rationale.

    `parent_context` propagates upstream attribution when smart_rce
    actions are user-initiated (e.g. one-shot discharge button — Etap 2F).
    The returned Context inherits `user_id` (audit: who triggered) and
    sets `parent_id = parent_context.id` (trace chain back to the click).
    Pass-through for automatic ticks (Ems.update_state from per-tick
    state mapper) — caller leaves `parent_context=None` and we create a
    fresh Context (no user attribution; smart_rce is the sole initiator).
    """
    ctx = Context(
        user_id=parent_context.user_id if parent_context else None,
        parent_id=parent_context.id if parent_context else None,
    )
    data: dict[str, Any] = {ATTR_PHASE: phase}
    if reason:
        data[ATTR_REASON] = reason
    hass.bus.async_fire(EVENT_SMART_RCE_ACTION, data, context=ctx)
    return ctx
