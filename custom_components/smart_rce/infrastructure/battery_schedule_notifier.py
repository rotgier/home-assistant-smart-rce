"""BatteryScheduleNotifier — telegram alerts on slot lifecycle events.

Subscribes to `BatteryScheduleEvent` emissions from
`BatterySchedule.compute_operation` (wired via `BatteryScheduleService.update`).
Fires `script.notify_alert_en` (with voice call, English TTS) for
`EMERGENCY` slots (evening discharge — user is awake) and
`script.notify_text` (text only) for `NORMAL` slots (morning slots
must not wake the user).

`DayRolled` events are NOT notified (internal-only midnight roll —
emitting telegram for every day boundary would be spam).

Hexagonal pattern: **driven adapter (outbound)** — application service
(`BatteryScheduleService`) decides WHEN to notify (passes domain events),
this adapter decides HOW (telegram channels). Dispatch is fire-and-forget
via `AsyncTaskRunner.run_background` — notify failures must not block
the per-tick update flow.

Messages are in English (emoji + title + descriptive message with key
data) — paired with `script.notify_alert_en` voice call (en-US TTS).
Other automations in the HA config remain Polish (paired with the
original `script.notify_alert`).
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, callback

from ..domain.battery_schedule import BatteryScheduleEvent, NotificationLevel
from .async_task_runner import AsyncTaskRunner

_LOGGER = logging.getLogger(__name__)

NOTIFY_ALERT_SCRIPT = "script.notify_alert_en"
NOTIFY_TEXT_SCRIPT = "script.notify_text"

# English labels for DisengageReason Literal — keep keys in sync with
# battery_schedule.py if new reasons are added.
_DISENGAGE_REASON_LABEL: dict[str, str] = {
    "target_reached": "target SoC reached",
    "window_ended": "time window ended",
    "disabled": "slot disabled",
    "expired": "deadline expired",
    "cancelled": "cancelled by user",
}


class BatteryScheduleNotifier:
    """Driven adapter — fires telegram for SlotEngaged/SlotDisengaged events."""

    def __init__(self, hass: HomeAssistant, tasks: AsyncTaskRunner) -> None:
        self._hass = hass
        self._tasks = tasks

    @callback
    def notify(self, event: BatteryScheduleEvent) -> None:
        """Spawn fire-and-forget background task for given event.

        DayRolled events are skipped here — render() returns None for them.
        Background tasks auto-cancel on entry unload (OK: telegram is
        best-effort, not state-critical).
        """
        self._tasks.run_background(
            self._dispatch(event),
            name=f"smart_rce_schedule_notify_{type(event).__name__}",
        )

    async def _dispatch(self, event: BatteryScheduleEvent) -> None:
        rendered = self._render(event)
        if rendered is None:
            return
        title, message, level = rendered
        script_entity = (
            NOTIFY_ALERT_SCRIPT
            if level == NotificationLevel.EMERGENCY
            else NOTIFY_TEXT_SCRIPT
        )
        try:
            await self._hass.services.async_call(
                "script",
                "turn_on",
                {
                    "entity_id": script_entity,
                    "variables": {"title": title, "message": message},
                },
                blocking=False,
            )
        except Exception:  # noqa: BLE001 — defensive: notify failure must not crash
            _LOGGER.exception("BatteryScheduleNotifier: %s call failed", script_entity)

    @staticmethod
    def _render(
        event: BatteryScheduleEvent,
    ) -> tuple[str, str, NotificationLevel] | None:
        """Render event → (title, message, level) or None to skip notify.

        Uses `type(event).__name__` string compare instead of `isinstance` —
        same pattern as `Direction.is_discharge` (CLAUDE.md cross-cutting
        rule). `isinstance` works against class identity, which `live_reload()`
        breaks: the old persisted class object becomes != to the re-imported
        new one, and an instance built BEFORE reload fails `isinstance(NEW)`
        even though structurally identical. Today both event and notifier are
        created in the same tick (transient, post-reload imports), so
        `isinstance` would technically work — string compare is defensive.
        """
        event_type = type(event).__name__
        if event_type == "SlotEngaged":
            level = event.slot.value.notification_level
            return (
                f"🔋 BatterySchedule: {event.slot.name} start",
                f"Slot {event.slot.name} started at SoC={event.soc:.1f}%.",
                level,
            )
        if event_type == "SlotDisengaged":
            level = event.slot.value.notification_level
            reason_label = _DISENGAGE_REASON_LABEL.get(event.reason, event.reason)
            return (
                f"🏁 BatterySchedule: {event.slot.name} end",
                (
                    f"Slot {event.slot.name} ended at SoC={event.soc:.1f}%, "
                    f"reason: {reason_label}."
                ),
                level,
            )
        if event_type == "DayRolled":
            return None  # midnight roll — internal only, no telegram
        if event_type == "OneShotStarted":
            op = event.operation
            return (
                f"⚡ One-Shot {op.direction.name} start",
                (
                    f"Ad-hoc {op.direction.name.lower()} to {op.target_soc:.0f}% "
                    f"until {op.end_at.strftime('%H:%M')}."
                ),
                NotificationLevel.NORMAL,
            )
        if event_type == "OneShotEnded":
            op = event.operation
            reason_label = _DISENGAGE_REASON_LABEL.get(event.reason, event.reason)
            return (
                f"🏁 One-Shot {op.direction.name} end",
                (
                    f"Ad-hoc {op.direction.name.lower()} ended, "
                    f"reason: {reason_label}."
                ),
                NotificationLevel.NORMAL,
            )
        _LOGGER.warning("BatteryScheduleNotifier: unhandled event type %s", event_type)
        return None
