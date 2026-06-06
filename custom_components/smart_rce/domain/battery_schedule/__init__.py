"""Public API for the battery schedule domain.

`BatterySchedule` is the aggregate root — driving `ems_interventions_blocked`
and producing `BatteryOperation` per tick. Supporting types are split across
sibling modules per concern; re-exports here keep external import paths stable.
"""

from .commands import (
    CancelOneShotCommand,
    OneShotParamsCommand,
    Scope,
    SetOneShotEndTimeCommand,
    SetOneShotTargetSocCommand,
    SetSlotBehaviorCommand,
    SetSlotEnabledCommand,
    SetSlotEndCommand,
    SetSlotStartCommand,
    SetSlotTargetSocCommand,
    SlotCommand,
    StartOneShotCommand,
)
from .direction import Direction, RateZone
from .entry import (
    BatteryScheduleEntry,
    DisengageReason,
    NotificationLevel,
    SlotBehavior,
    SlotKind,
    SlotProfile,
)
from .events import (
    BatteryScheduleEvent,
    DayRolled,
    OneShotEnded,
    OneShotStarted,
    SlotDisengaged,
    SlotEngaged,
)
from .oneshot import OneShotDisengageReason, OneShotOperation, OneShotParams
from .operation import BatteryOperation, BatteryScheduleInput
from .schedule import BatterySchedule

__all__ = [
    "BatteryOperation",
    "BatterySchedule",
    "BatteryScheduleEntry",
    "BatteryScheduleEvent",
    "BatteryScheduleInput",
    "CancelOneShotCommand",
    "DayRolled",
    "Direction",
    "DisengageReason",
    "NotificationLevel",
    "OneShotDisengageReason",
    "OneShotEnded",
    "OneShotOperation",
    "OneShotParams",
    "OneShotParamsCommand",
    "OneShotStarted",
    "RateZone",
    "Scope",
    "SetOneShotEndTimeCommand",
    "SetOneShotTargetSocCommand",
    "SetSlotBehaviorCommand",
    "SetSlotEnabledCommand",
    "SetSlotEndCommand",
    "SetSlotStartCommand",
    "SetSlotTargetSocCommand",
    "SlotBehavior",
    "SlotCommand",
    "SlotDisengaged",
    "SlotEngaged",
    "SlotKind",
    "SlotProfile",
    "StartOneShotCommand",
]
