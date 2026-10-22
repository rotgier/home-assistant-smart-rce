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
    is_day_off,
)
from .events import (
    BatteryScheduleEvent,
    DayRolled,
    OneShotEnded,
    OneShotStarted,
    SlotDisengaged,
    SlotEngaged,
)
from .morning_charge import MorningChargeFloor
from .morning_discharge import MorningDischargeBranch, MorningDischargePlan
from .oneshot import OneShotDisengageReason, OneShotOperation, OneShotParams
from .operation import BatteryOperation, BatteryScheduleInput
from .schedule import BatterySchedule
from .session import SlotSession, UpcomingRun
from .t1_reserve import ReserveWindow, T1SocReserve

__all__ = [
    "BatteryOperation",
    "BatterySchedule",
    "BatteryScheduleEntry",
    "BatteryScheduleEvent",
    "BatteryScheduleInput",
    "CancelOneShotCommand",
    "DayRolled",
    "Direction",
    "MorningDischargeBranch",
    "MorningChargeFloor",
    "MorningDischargePlan",
    "DisengageReason",
    "NotificationLevel",
    "OneShotDisengageReason",
    "OneShotEnded",
    "OneShotOperation",
    "OneShotParams",
    "OneShotParamsCommand",
    "OneShotStarted",
    "ReserveWindow",
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
    "T1SocReserve",
    "SlotProfile",
    "SlotSession",
    "StartOneShotCommand",
    "UpcomingRun",
    "is_day_off",
]
