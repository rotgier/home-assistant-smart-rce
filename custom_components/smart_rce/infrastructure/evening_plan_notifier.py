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
FAILURE_TITLE: Final = "EMS — plan wieczorny NIE ZADZIAŁAŁ"


async def notify_evening_plan(
    hass: HomeAssistant,
    plan: EveningPlan,
    *,
    application: PlanApplication,
    for_tomorrow: bool,
    answer_always: bool = False,
) -> None:
    """Send the outcome to Telegram. Never raises — the plan outranks the report.

    A scheduled run stays silent when nothing moved: restarts between 15:00
    and 21:00 re-trigger the afternoon sweep, and repeating "already current"
    each time would train the reader to ignore the channel.

    A button press always answers (`answer_always`), because someone is
    waiting to learn whether it did anything.
    """
    if application.outcome is PlanOutcome.ALREADY_CURRENT and not answer_always:
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


async def notify_evening_plan_failed(
    hass: HomeAssistant, *, run: str, error: BaseException
) -> None:
    """Say out loud that a run died, because the alternative is silence.

    A run that raises leaves a traceback in the log and nothing else: no slots
    change, no message arrives, and the evening simply keeps yesterday's
    windows. That is how two runs were lost on 02.10 and very possibly how the
    25.09 evening went missing — the logs had rolled over by the time anyone
    looked. The reader cannot act on a log they do not read, so the channel
    that carries good news has to carry this too.

    Never raises: reporting a failure must not become a second one.
    """
    message = (
        f"{run}: {type(error).__name__} — {error}\n"
        "Sloty zostały bez zmian. Szczegóły w logu Core."
    )
    try:
        await hass.services.async_call(
            NOTIFY_DOMAIN,
            NOTIFY_SERVICE,
            {"title": FAILURE_TITLE, "message": message},
            blocking=False,
        )
    except Exception:  # noqa: BLE001 - reporting must never break planning
        _LOGGER.exception("Evening plan failure notification failed")
