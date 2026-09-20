"""What became of a plan that was handed to the schedule."""

from __future__ import annotations

from enum import StrEnum


class PlanOutcome(StrEnum):
    """Result of offering an evening plan to the aggregate.

    Kept apart from a plain bool because "the slots already said this" and "I
    left your settings alone" read very differently in a Telegram message.
    """

    APPLIED = "applied"
    """Slots were changed to match the plan."""

    ALREADY_CURRENT = "already_current"
    """Plan was adopted, but the slots already matched it."""

    DEFERRED_TO_MANUAL = "deferred_to_manual"
    """Plan stood down — the user had set the evening by hand today."""
