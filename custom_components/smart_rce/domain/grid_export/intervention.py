"""Intervention Protocol + result VOs + InterventionDirection enum.

Common interface for active intervention sessions (POSITIVE / NEGATIVE).
Each intervention class lives in its own module (negative.py, positive.py)
and is wired together by manager.py via Protocol-based duck typing.

DDD framing:
- Intervention = Entity (active session, mutable state, lifecycle: born on
  try_enter, mutated on continue_or_exit, dies when manager sets _active=None)
- EntryResult, ContinueResult = Value Objects (immutable, frozen dataclasses)
- InterventionDirection = enum (categorizes Entity type, exposed to sensors)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Final, Protocol

if TYPE_CHECKING:
    from custom_components.smart_rce.domain.input_state import InputState


class InterventionDirection(StrEnum):
    """Direction of active GridExportManager intervention.

    POSITIVE — hourly balance excessively positive (export > 0.06 kWh),
    manager forces CHARGE_BATTERY (or STANDBY at low PV) to consume balance.

    NEGATIVE — hourly balance negative (net import), manager forces adaptive
    charge/discharge to stabilize meter at ≈ +1500W export.
    """

    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"


class Intervention(Protocol):
    """Active intervention session — common interface POSITIVE / NEGATIVE.

    Lifecycle:
    1. Manager calls `Cls.try_enter(state)` → EntryResult (factory)
    2. Active intervention: manager calls `instance.continue_or_exit(state)`
       → ContinueResult on every update
    3. Exit: manager sets `self._active = None`

    Manager contract: BEFORE calling try_enter / continue_or_exit it checks
    global guards (balance range, ems_override, hour rollover, end_of_hour,
    too_late_in_hour, other_automation_active). Intervention only checks
    intervention-specific preconditions.
    """

    direction: ClassVar[InterventionDirection]
    recommended_mode: str
    recommended_xset: int
    started_hour: int
    last_reason: str

    @classmethod
    def try_enter(cls, state: InputState) -> EntryResult: ...

    def continue_or_exit(self, state: InputState) -> ContinueResult: ...


@dataclass(frozen=True)
class EntryResult:
    """try_enter result — either new intervention, or block reason.

    Factory result: intervention=None when intervention-specific gate blocks
    entry (e.g. SoC out of range, toggle off, pre_charge_window).
    """

    intervention: Intervention | None
    block_reason: str | None

    @property
    def is_blocked(self) -> bool:
        return self.intervention is None

    @classmethod
    def blocked(cls, reason: str) -> EntryResult:
        return cls(intervention=None, block_reason=reason)

    @classmethod
    def entered(cls, intervention: Intervention) -> EntryResult:
        return cls(intervention=intervention, block_reason=None)


@dataclass(frozen=True)
class ContinueResult:
    """continue_or_exit result — None exit_reason means continue.

    Continue case: intervention has mutated its fields in-place
    (recommended_mode, recommended_xset, last_reason). Manager only syncs
    last_decision_reason from self._active.last_reason.

    Exit case: manager sets self._active = None and writes exit_reason
    to last_decision_reason.
    """

    exit_reason: str | None

    @property
    def is_exit(self) -> bool:
        return self.exit_reason is not None

    @classmethod
    def exit_with(cls, reason: str) -> ContinueResult:
        return cls(exit_reason=reason)


CONTINUE: Final = ContinueResult(exit_reason=None)
"""Singleton sentinel for continue case (intervention mutated in place)."""
