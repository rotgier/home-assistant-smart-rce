"""Commands — mutating actions (Command pattern, Open/Closed).

Two protocols + 9 concrete commands. Aggregate (`BatterySchedule`) owns the
read-modify-write lifecycle; each Command owns its transformation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Literal, Protocol

from .direction import Direction
from .entry import BatteryScheduleEntry, SlotBehavior, SlotKind
from .oneshot import OneShotParams

Scope = Literal["today", "tomorrow"]


class SlotCommand(Protocol):
    """A mutating action targeting a single slot entry in the aggregate.

    Aggregate calls `apply_to_entry(current)` to obtain the new Entry value;
    aggregate owns the read-modify-write lifecycle, Command owns the
    transformation. Adding a new editable field = new Command class (no
    changes to aggregate or service — Open/Closed).
    """

    scope: Scope
    kind: SlotKind

    def apply_to_entry(self, entry: BatteryScheduleEntry) -> BatteryScheduleEntry: ...


@dataclass(frozen=True)
class SetSlotEnabledCommand:
    scope: Scope
    kind: SlotKind
    value: bool

    def apply_to_entry(self, entry: BatteryScheduleEntry) -> BatteryScheduleEntry:
        return entry.with_enabled(self.value)


@dataclass(frozen=True)
class SetSlotStartCommand:
    scope: Scope
    kind: SlotKind
    value: time

    def apply_to_entry(self, entry: BatteryScheduleEntry) -> BatteryScheduleEntry:
        return entry.with_start(self.value)


@dataclass(frozen=True)
class SetSlotEndCommand:
    scope: Scope
    kind: SlotKind
    value: time

    def apply_to_entry(self, entry: BatteryScheduleEntry) -> BatteryScheduleEntry:
        return entry.with_end(self.value)


@dataclass(frozen=True)
class SetSlotTargetSocCommand:
    scope: Scope
    kind: SlotKind
    value: float

    def apply_to_entry(self, entry: BatteryScheduleEntry) -> BatteryScheduleEntry:
        return entry.with_target_soc(self.value)


@dataclass(frozen=True)
class SetSlotBehaviorCommand:
    scope: Scope
    kind: SlotKind
    value: SlotBehavior

    def apply_to_entry(self, entry: BatteryScheduleEntry) -> BatteryScheduleEntry:
        return entry.with_behavior(self.value)


@dataclass(frozen=True)
class StartOneShotCommand:
    """Execute one-shot operation in given direction using stored params."""

    direction: Direction


@dataclass(frozen=True)
class CancelOneShotCommand:
    """Cancel active one-shot operation (no-op if none)."""


class OneShotParamsCommand(Protocol):
    """A mutating action targeting OneShotParams for one direction.

    Same pattern as `SlotCommand.apply_to_entry`: Command owns the
    transformation, aggregate owns the read-modify-write lifecycle and
    dict storage. New editable param field = new Command class (no
    changes to aggregate or service — Open/Closed).
    """

    direction: Direction

    def apply_to_params(self, params: OneShotParams) -> OneShotParams: ...


@dataclass(frozen=True)
class SetOneShotTargetSocCommand:
    direction: Direction
    value: float

    def apply_to_params(self, params: OneShotParams) -> OneShotParams:
        return params.with_target_soc(self.value)


@dataclass(frozen=True)
class SetOneShotEndTimeCommand:
    direction: Direction
    value: time

    def apply_to_params(self, params: OneShotParams) -> OneShotParams:
        return params.with_end_time(self.value)
