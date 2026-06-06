"""Events — domain happenings emitted by `BatterySchedule.compute_operation`.

Notifier dispatches by isinstance — separate handler per event type.
Reason enums (`DisengageReason`, `OneShotDisengageReason`) live with their
producing classes (`entry.py` / `oneshot.py`) — events merely reference them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .entry import DisengageReason, SlotKind
from .oneshot import OneShotDisengageReason, OneShotOperation


@dataclass(frozen=True)
class BatteryScheduleEvent:
    """Base marker class for domain events emitted by compute_operation.

    Notifier dispatches by isinstance — separate handler per type.
    """


@dataclass(frozen=True)
class SlotEngaged(BatteryScheduleEvent):
    """A slot just became active — orchestrator started engaging it."""

    slot: SlotKind
    soc: float
    at: datetime


@dataclass(frozen=True)
class SlotDisengaged(BatteryScheduleEvent):
    """A slot just stopped being active.

    `reason` distinguishes why:
    - "target_reached" — SoC reached target (normal completion)
    - "window_ended" — `now` moved past `end` (window timeout)
    - "disabled" — slot.enabled flipped to False mid-engagement
    """

    slot: SlotKind
    soc: float
    at: datetime
    reason: DisengageReason


@dataclass(frozen=True)
class DayRolled(BatteryScheduleEvent):
    """Midnight crossing detected — tomorrow_* shifted to today_*."""

    from_date: date
    to_date: date


@dataclass(frozen=True)
class OneShotStarted(BatteryScheduleEvent):
    """One-shot operation just started — user pressed Execute."""

    operation: OneShotOperation
    at: datetime


@dataclass(frozen=True)
class OneShotEnded(BatteryScheduleEvent):
    """One-shot operation ended.

    `reason` distinguishes:
    - "target_reached" — SoC reached target (normal completion)
    - "expired" — `now >= end_at` reached
    - "cancelled" — user pressed Cancel button
    """

    operation: OneShotOperation
    reason: OneShotDisengageReason
    at: datetime
