"""What became of a plan that was handed to the schedule, and what it changed."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..domain.battery_schedule import BatteryScheduleEntry, SlotKind


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


@dataclass(frozen=True)
class SlotChange:
    """One evening slot before and after the plan touched it."""

    kind: SlotKind
    before: BatteryScheduleEntry
    after: BatteryScheduleEntry

    @property
    def was_disabled(self) -> bool:
        return self.before.enabled and not self.after.enabled

    @property
    def was_enabled(self) -> bool:
        return not self.before.enabled and self.after.enabled


@dataclass(frozen=True)
class PlanApplication:
    """Outcome plus the slot-by-slot diff, so the report can be specific.

    Without the diff a notification can only say "ustawiono"; with it the
    message can name what moved, which matters most when the plan overrode
    something the user had set on purpose.
    """

    outcome: PlanOutcome
    changes: tuple[SlotChange, ...] = ()

    @property
    def changed_anything(self) -> bool:
        return bool(self.changes)
