"""Reports what the evening planner decided, through the existing notify script.

Shared by the scheduled runs and the dashboard button so both report the same
way — a plan applied by hand-press is no less worth knowing about than one
applied at 22:05.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from homeassistant.core import HomeAssistant

from ..application.plan_application import PlanApplication, PlanOutcome, SlotChange
from ..domain.battery_schedule import BatteryScheduleEntry, SlotKind

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
    application: PlanApplication,
    for_tomorrow: bool,
) -> None:
    """Send the outcome to Telegram. Never raises — the plan outranks the report.

    Silent when nothing moved: a restart between 15:00 and 21:00 re-runs the
    afternoon sweep, and repeating "already current" on every restart would
    train the reader to ignore the channel.
    """
    if application.outcome is PlanOutcome.ALREADY_CURRENT:
        _LOGGER.debug("Evening plan unchanged — no notification sent")
        return
    when = "jutro" if for_tomorrow else "dziś"
    message = f"{when}: {_OUTCOME_TEXT[application.outcome]} — {_describe(plan)}"
    if application.changes:
        message += "\n" + "\n".join(_describe_change(c) for c in application.changes)
    try:
        await hass.services.async_call(
            NOTIFY_DOMAIN,
            NOTIFY_SERVICE,
            {"title": NOTIFY_TITLE, "message": message},
            blocking=False,
        )
    except Exception:  # noqa: BLE001 - reporting must never break planning
        _LOGGER.exception("Evening plan notification failed")


def _describe_change(change: SlotChange) -> str:
    """One slot, before and after — so the reader sees what the plan moved."""
    label = (
        "wcześniejszy"
        if change.kind is SlotKind.DISCHARGE_EVENING_EARLY
        else "późniejszy"
    )
    if change.was_disabled:
        return f"• {label}: wyłączony (był {_window(change.before)})"
    if change.was_enabled:
        return f"• {label}: włączony {_window(change.after)}"
    return f"• {label}: {_window(change.before)} → {_window(change.after)}"


def _window(entry: BatteryScheduleEntry) -> str:
    return f"{entry.start:%H:%M}-{entry.end:%H:%M} do {entry.target_soc:.0f}%"


def _describe(plan: EveningPlan) -> str:
    """Plan in the user's language — windows, or why there are none."""
    if plan.windows:
        return " + ".join(
            f"{w.start:%H:%M}-{w.end:%H:%M} do {w.target_soc:.0f}%"
            for w in plan.windows
        )
    if plan.max_morning_price > 0:
        return (
            f"brak okna (rano jutro {plan.max_morning_price:.0f} zł/MWh brutto "
            f"bije wieczór)"
        )
    return "brak okna (żadna godzina nie przebija progu)"


# Message is read by a person on a phone, so it is in the user's language —
# unlike log lines and docstrings, which stay English.
_OUTCOME_TEXT: Final[dict[PlanOutcome, str]] = {
    PlanOutcome.APPLIED: "ustawiono",
    PlanOutcome.ALREADY_CURRENT: "bez zmian (już aktualne)",
    PlanOutcome.DEFERRED_TO_MANUAL: "zostawiono Twoje ustawienie ręczne",
}
