"""Reports what the evening planner decided, through the existing notify script.

Shared by the scheduled runs and the dashboard button so both report the same
way — a plan applied by hand-press is no less worth knowing about than one
applied at 22:05.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from ..domain.evening_plan import EveningPlan

_LOGGER = logging.getLogger(__name__)

NOTIFY_DOMAIN: Final = "script"
NOTIFY_SERVICE: Final = "notify_text"
NOTIFY_TITLE: Final = "EMS — plan wieczorny"


async def notify_evening_plan(
    hass: HomeAssistant,
    plan: EveningPlan,
    *,
    applied: bool,
    for_tomorrow: bool,
) -> None:
    """Send the outcome to Telegram. Never raises — the plan outranks the report."""
    when = "jutro" if for_tomorrow else "dziś"
    verb = "ustawiono" if applied else "bez zmian (ustawienie ręczne)"
    try:
        await hass.services.async_call(
            NOTIFY_DOMAIN,
            NOTIFY_SERVICE,
            {"title": NOTIFY_TITLE, "message": f"{when}: {verb} — {plan.reason}"},
            blocking=False,
        )
    except Exception:  # noqa: BLE001 - reporting must never break planning
        _LOGGER.exception("Evening plan notification failed")
