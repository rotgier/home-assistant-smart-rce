"""BatteryScheduleNotifier — telegram alerts on slot lifecycle events.

Subscribes to `BatteryScheduleEvent` emissions from
`BatterySchedule.compute_operation` (wired via `BatteryScheduleService.update`).
Fires `script.notify_alert` (with voice call) for `EMERGENCY` slots
(evening discharge — user is awake) and `script.notify_text` (text only)
for `NORMAL` slots (morning slots — would wake user up).

`DayRolled` events are NOT notified (internal-only midnight roll —
emitting telegram for every day boundary would be spam).

Hexagonal pattern: **driven adapter (outbound)** — application service
(`BatteryScheduleService`) decides WHEN to notify (passes domain events),
this adapter decides HOW (telegram channels). Dispatch is fire-and-forget
via `AsyncTaskRunner.run_background` — notify failures must not block
the per-tick update flow.

Polish messages match `automations.yaml` convention (emoji + title +
descriptive message with key data).
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, callback

from ..domain.battery_schedule import (
    BatteryScheduleEvent,
    DayRolled,
    NotificationLevel,
    SlotDisengaged,
    SlotEngaged,
)
from .async_task_runner import AsyncTaskRunner

_LOGGER = logging.getLogger(__name__)

NOTIFY_ALERT_SCRIPT = "script.notify_alert"
NOTIFY_TEXT_SCRIPT = "script.notify_text"

# Polish translations for disengage reasons (DisengageReason Literal in
# battery_schedule.py — keep keys in sync if new reasons are added).
_DISENGAGE_REASON_PL: dict[str, str] = {
    "target_reached": "cel SoC osiągnięty",
    "window_ended": "okno czasowe zakończone",
    "disabled": "slot wyłączony",
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
        """Render event → (title, message, level) or None to skip notify."""
        if isinstance(event, SlotEngaged):
            level = event.slot.value.notification_level
            return (
                f"🔋 BatterySchedule: {event.slot.name} start",
                f"Slot {event.slot.name} rozpoczęty, SoC={event.soc:.1f}%.",
                level,
            )
        if isinstance(event, SlotDisengaged):
            level = event.slot.value.notification_level
            reason_pl = _DISENGAGE_REASON_PL.get(event.reason, event.reason)
            return (
                f"🏁 BatterySchedule: {event.slot.name} koniec",
                (
                    f"Slot {event.slot.name} zakończony, "
                    f"SoC={event.soc:.1f}%, powód: {reason_pl}."
                ),
                level,
            )
        if isinstance(event, DayRolled):
            return None  # midnight roll — internal only, no telegram
        _LOGGER.warning(
            "BatteryScheduleNotifier: unhandled event type %s",
            type(event).__name__,
        )
        return None
