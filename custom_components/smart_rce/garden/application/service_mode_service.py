"""ServiceModeService — garden-wide "hands-on maintenance" suppression flag.

When ON (user physically handling Luba — cleaning grass off the deck, service),
the garden stops acting on its own: the planner reports `should_start=False` (so
the auto-dispatch automation never fires a start/resume) and the mowing hold
skips pushing non-work overrides to the device. HA notification automations also
gate on `switch.garden_service_mode` so they do not spam during handling.

Deliberately NOT persisted (in-memory, resets to OFF on restart): service mode is
a transient operational state, not a tuned policy — a restart should resume
normal operation, and a forgotten persisted flag would silently idle Luba for
days. (An auto-timeout is a planned future addition — see
`plans/garden-planner-estimation-tuning.md`, Issue 6.)

Listenable, no repository: the planner service, the mowing hold service and the
switch entity subscribe and recompute / refresh when the flag flips.
"""

from __future__ import annotations

from custom_components.smart_rce.application.listenable import Listenable
from homeassistant.core import callback


class ServiceModeService(Listenable):
    """In-memory garden service-mode flag (maintenance suppression)."""

    def __init__(self) -> None:
        super().__init__()
        self._active = False

    @property
    def is_active(self) -> bool:
        """True while service mode suppresses garden auto-actions + alerts."""
        return self._active

    @callback
    def set_active(self, active: bool) -> None:
        """Toggle service mode; notify listeners (planner/hold/switch) on change."""
        if active == self._active:
            return
        self._active = active
        self._notify_all()
