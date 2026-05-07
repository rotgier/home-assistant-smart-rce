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
    """Kierunek aktywnej interwencji GridExportManager.

    POSITIVE — bilans hourly nadmiernie pozytywny (eksport > 0.06 kWh),
    manager wymusza CHARGE_BATTERY (lub STANDBY przy niskim PV) by zjeść saldo.

    NEGATIVE — bilans hourly negatywny (import netto), manager wymusza
    adaptive charge/discharge by ustabilizować meter ≈ +1500W eksport.
    """

    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"


class Intervention(Protocol):
    """Active intervention session — common interface POSITIVE / NEGATIVE.

    Lifecycle:
    1. Manager wywołuje `Cls.try_enter(state)` → EntryResult (factory)
    2. Active intervention: manager wywołuje `instance.continue_or_exit(state)`
       → ContinueResult na każdym update
    3. Exit: manager ustawia `self._active = None`

    Manager ma kontrakt: PRZED wywołaniem try_enter / continue_or_exit
    sprawdza global guards (balance range, ems_override, hour rollover,
    end_of_hour, too_late_in_hour, other_automation_active). Intervention
    sprawdza tylko intervention-specific preconditions.
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
    """Wynik try_enter — albo nowa intervention, albo block reason.

    Factory result: intervention=None gdy intervention-specific gate blokuje
    entry (np. SoC poza zakresem, toggle off, pre_charge_window).
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
    """Wynik continue_or_exit — None exit_reason oznacza continue.

    Continue case: intervention sam zmutował swoje pola in-place
    (recommended_mode, recommended_xset, last_reason). Manager tylko
    sync'uje last_decision_reason z self._active.last_reason.

    Exit case: manager ustawia self._active = None i zapisuje exit_reason
    do last_decision_reason.
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
