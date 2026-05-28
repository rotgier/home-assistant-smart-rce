"""ApplyGuard — anti-spam + telegram alert helper for driven actuators.

Shared safety layer for actuators that write to Goodwe via `scene.apply`
(`DodPolicyActuator`, `GoodweEmsActuator`). Two responsibilities:

1. **Anti-spam counter**: tracks failed applies per hour. After
   `max_failed_per_hour` failures, `should_skip()` returns True until the
   hour boundary — stops the actuator from hammering a broken inverter.
   Counter auto-resets on hour change and on `record_success()`.

2. **Telegram alert dispatch**: failures fire `script.notify_alert` with a
   structured title/message. When the limit is hit, a separate "muting
   retries until next hour" alert is sent (single, not per-failure).

Logbook attribution is NOT this helper's concern — that's covered by
`Context(user_id=...)` passed to `scene.apply` (the resulting state_changed
event renders as "Changed by Smart RCE" in HA logbook).
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

NOTIFY_ALERT_SCRIPT = "script.notify_alert"
DEFAULT_MAX_FAILED_PER_HOUR = 10


class ApplyGuard:
    """Per-hour failure counter + telegram alerts for actuators."""

    def __init__(
        self,
        hass: HomeAssistant,
        actuator_name: str,
        *,
        max_failed_per_hour: int = DEFAULT_MAX_FAILED_PER_HOUR,
    ) -> None:
        self._hass = hass
        self._name = actuator_name
        self._max = max_failed_per_hour
        self._failed_count: int = 0
        self._failed_hour: int | None = None

    def should_skip(self) -> bool:
        """Return True when the per-hour failure threshold has been reached.

        Side effect: resets counter when the clock hour changes.
        """
        now_hour = dt_util.now().hour
        if self._failed_hour != now_hour:
            self._failed_count = 0
            self._failed_hour = now_hour
        if self._failed_count >= self._max:
            _LOGGER.debug(
                "%s: skipping — %d failed applies in hour %d, "
                "waiting for hour boundary",
                self._name,
                self._failed_count,
                now_hour,
            )
            return True
        return False

    def record_success(self) -> None:
        """Reset counter on successful apply (transient failures don't accumulate)."""
        self._failed_count = 0

    async def record_failure(self, *, title: str, message: str) -> None:
        """Increment counter + fire telegram alert. Hits mute alert at threshold."""
        self._failed_count += 1
        await self._notify(title, message)
        if self._failed_count == self._max:
            await self._notify(
                title=f"Smart RCE: limit prób ({self._name})",
                message=(
                    f"Osiągnięto {self._max} nieudanych prób zapisu w godzinie "
                    f"{self._failed_hour}. Wstrzymuję ponowne próby do kolejnej godziny."
                ),
            )

    async def _notify(self, title: str, message: str) -> None:
        try:
            await self._hass.services.async_call(
                "script",
                "turn_on",
                {
                    "entity_id": NOTIFY_ALERT_SCRIPT,
                    "variables": {"title": title, "message": message},
                },
                blocking=False,
            )
        except Exception:  # noqa: BLE001 — defensive: don't crash actuator on notify failure
            _LOGGER.exception("%s: notify_alert call failed", self._name)
