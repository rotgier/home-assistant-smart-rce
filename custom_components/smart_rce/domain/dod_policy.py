"""DoD Policy — single source of truth for inverter DoD register value.

Maps phase (time + flags) + block_discharge hysteresis + user override →
target_dod (0..100). Replaces 4 fragile time-triggered HA automations:

- `Set SOC 90 at 7 and at 19`           → delegating phases (block_discharge fns)
- `Set Min SOC to 100 Afternoon`        → phase AFTERNOON_STATIC (direct 0)
- `Set Min SOC to 100 Evening`          → phase NIGHT_PRESERVE (direct 0)
- `ems-set-dod-from-block-discharge`    → replaced by DodPolicyActuator
                                          (driven adapter, ADR-019 pattern)

Self-healing on restart: each tick reads time + flags, computes phase from
scratch. No timing-sensitive trigger needed — DodPolicy is event-driven via
EMS update flow. UNKNOWN phase (transient input gaps post-restart) preserves
persisted target_dod until inputs settle.

Phase classification + decisions:

- **OVERRIDE** (`dod_override >= 0`): user-forced value. Auto-expires on
  phase boundary (current phase ≠ phase at activation).
- **INTERVENTIONS_BLOCKED** (`ems_interventions_blocked=True`, passed as
  kwarg from Ems — sourced from BatterySchedule.ems_interventions_blocked):
  smart_rce's internal interventions blocked → DoD=90, lets external schedule
  / user override drive discharge through.
- **WORKDAY_PRE_CHARGE** (7:00 → start_charge_hour, workday): delegates to
  `block_pre_charge` — hysteresis 100/50 Wh + instant_surplus extension.
- **WORKDAY_POST_CHARGE** (start_charge_hour → 13:00, workday): delegates
  to `block_post_charge` — dual-trigger (instant + hourly). Two-tier defense
  with POSITIVE intervention (POSITIVE first line >60 Wh, block_post_charge
  second line >=100 Wh / instant_surplus when POSITIVE cannot absorb, e.g.
  SoC=100% / BMS clamp / sustained surplus).
- **EVENING_DISCHARGE** (T2 of a workday): direct DoD=90 — the house draws
  the battery through the expensive zone; the evening slots sell
  (`ems_interventions_blocked`).
- Every other hour from 13:00 to 07:00 — afternoon, the night, and on a day
  off the whole evening — follows one rule (user's decision 17.10.2026,
  replacing "max upcoming RCE > 700"; the day-off evening 19-22 is gone):
  - **HOLD_FOR_SALE**: a sale is planned — an evening slot still ahead today,
    or tomorrow morning's plan at or above the export bar
    (`number.ems_export_threshold`) with energy above its target. Direct
    DoD=0: the battery waits for it.
  - **RESERVE_FLOOR**: a workday morning ahead. The house may draw down to
    that morning's T1 reserve (DoD = 100 - reserve), no further.
  - **RELEASED**: otherwise the house may draw down to 10% (DoD=90).
  - In RESERVE_FLOOR and RELEASED the battery does not discharge while PV is
    exporting anyway (`block_afternoon_dynamic` hysteresis → DoD=0).
  - **NIGHT_PRESERVE**: a workday morning ahead but no reserve known for it
    (from 14:00, `T1SocReserve.TOMORROW_FROM_HOUR`) — hold everything, as
    before 17.10.2026.
- **WEEKEND_MORNING** (7:00 → 13:00, weekend): direct DoD=0 — passive PV
  capture (RCE typically flat, no expensive hours to protect surplus).
- **UNKNOWN** (inputs missing): keep persisted state.

Coordination with GridExportManager (POSITIVE / NEGATIVE intervention):

    | Phase              | block_discharge       | POSITIVE         | NEGATIVE |
    |--------------------|-----------------------|------------------|----------|
    | INTERVENTIONS_BLOCKED | False (always)     | exits            | exits    |
    | WORKDAY_PRE        | hysteresis            | blocked          | yes      |
    | WORKDAY_POST       | dual-trigger          | yes (1st line)   | yes      |
    | HOLD_FOR_SALE      | False (DoD=0 direct)  | rare (SoC ceil)  | yes      |
    | RESERVE_FLOOR      | dual-trigger          | yes (1st line)   | yes      |
    | RELEASED           | dual-trigger          | yes (1st line)   | yes      |
    | EVENING_DISCHARGE  | False (DoD=90 direct) | rare             | yes      |
    | NIGHT_PRESERVE     | False (DoD=0 direct)  | rare             | yes      |
    | WEEKEND_MORNING    | False (DoD=0 direct)  | yes              | yes      |

GridExport intervention thresholds (hourly net export, Wh; details in
`grid_export/positive.py` + `negative.py`):

- POSITIVE: entry > +60, exit < +50 (deadband +50..+60), SoC entry ≤ 99 /
  exit ≥ 100.
- NEGATIVE: entry pre-45min < -50, post-45min < 0, exit > 0; SoC hard floor
  ≤ 10. DoD-floor handling with hysteresis.

Override semantics: `input_number.ems_dod_override` ≥ 0 takes priority over
phase logic. `_override_set_phase` records phase at activation; expires when
current phase != activation phase (auto-resume of normal logic). State
survives HA restart via `dod_policy_repository`.

See `context/target_soc_algorithm.md` for broader context.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Final

from ..tariff import evening_peak
from .battery_schedule.t1_reserve import T1SocReserve
from .block_discharge import (
    block_afternoon_dynamic,
    block_post_charge,
    block_pre_charge,
)
from .bucket import PV_WINDOW_HOURS

if TYPE_CHECKING:
    from datetime import time

    from .input_state import InputState


class Phase(Enum):
    """DoD policy phases. Dispatched in priority order; first match wins."""

    OVERRIDE = "override"
    INTERVENTIONS_BLOCKED = "interventions_blocked"
    # When True, BatterySchedule.ems_interventions_blocked active → smart_rce's
    # internal EMS interventions (POSITIVE/NEGATIVE block_discharge phase rules)
    # are blocked, DodPolicy stays at DoD=90 so explicit schedule/user override
    # can drive discharge through. Renamed from EMS_ALLOW_DISCHARGE (Etap 0).
    WORKDAY_PRE_CHARGE = "workday_pre_charge"  # 7:00..start_charge, workday
    WORKDAY_POST_CHARGE = "workday_post_charge"  # start_charge..13:00, workday
    EVENING_DISCHARGE = "evening_discharge"  # T2 of a workday
    HOLD_FOR_SALE = "hold_for_sale"  # 13:00..07:00 off T2, a sale planned
    RESERVE_FLOOR = "reserve_floor"  # 13:00..07:00 off T2, workday morning ahead
    RELEASED = "released"  # 13:00..07:00 off T2, otherwise
    NIGHT_PRESERVE = "night_preserve"  # workday morning ahead, reserve unknown
    WEEKEND_MORNING = "weekend_morning"  # 7:00..13:00, weekend
    UNKNOWN = "unknown"  # fallback (missing inputs)


# Phases where DodPolicy delegates DoD to BatteryManager.block_discharge
# (block=True → DoD=0, block=False → DoD=90).
DELEGATING_PHASES: frozenset[Phase] = frozenset(
    {
        Phase.WORKDAY_PRE_CHARGE,
        Phase.WORKDAY_POST_CHARGE,
        Phase.RESERVE_FLOOR,
        Phase.RELEASED,
    }
)

# Phases with direct (non-delegating) DoD rule.
DIRECT_PHASE_DOD: dict[Phase, int] = {
    Phase.INTERVENTIONS_BLOCKED: 90,  # smart_rce interventions blocked — DoD=90 so
    # explicit schedule/user override can discharge through.
    Phase.EVENING_DISCHARGE: 90,  # workday T2 — the house draws the battery
    Phase.HOLD_FOR_SALE: 0,  # a sale planned — keep it for that
    Phase.NIGHT_PRESERVE: 0,  # workday morning ahead, reserve unknown
    Phase.WEEKEND_MORNING: 0,  # weekend rano — passive PV capture
}

DEFAULT_DOD: int = 90  # safe fallback when inputs incomplete

# Mapping delegating phase → stateless hysteresis function.
_PHASE_TO_BLOCK_FN = {
    Phase.WORKDAY_PRE_CHARGE: block_pre_charge,
    Phase.WORKDAY_POST_CHARGE: block_post_charge,
    Phase.RESERVE_FLOOR: block_afternoon_dynamic,
    Phase.RELEASED: block_afternoon_dynamic,
}


@dataclass
class DodPolicy:
    """Computes target_dod per tick based on phase + hysteresis + override.

    Persistent state (across HA restart):
    - target_dod: last emitted value (informational + UNKNOWN keep-state source)
    - current_phase: phase computed at last tick (diagnostic + override expiry)
    - _override_set_phase: phase in which override was activated (for expiry)
    - _prev_block: hysteresis keep-state for delegating phases (block_discharge)
    """

    target_dod: int = DEFAULT_DOD
    current_phase: Phase = Phase.UNKNOWN
    _override_set_phase: Phase | None = None
    _prev_block: bool = False

    @property
    def prev_block(self) -> bool:
        """Hysteresis keep-state for delegating phases (diagnostic read)."""
        return self._prev_block

    def update(
        self,
        state: InputState,
        *,
        ems_interventions_blocked: bool = False,
        start_charge_hour_override: time | None = None,
        sale_planned: bool | None = None,
        reserve_floor: int | None = None,
    ) -> None:
        """Compute target_dod for this tick.

        Reads InputState (time, exported_energy, pv_5min, is_workday,
        is_workday_tomorrow) plus kwargs from Ems:
        - `ems_interventions_blocked` from BatteryScheduleService
        - `start_charge_hour_override` from BatteryChargeService (Etap B'-2)
        - `sale_planned` — BatteryScheduleService.sale_planned_ahead (an
          evening slot still ahead today, or the morning ahead's plan at or
          above the export bar); None while unknown
        - `reserve_floor` — the T1 reserve of the morning ahead, None if none

        Mutates target_dod + current_phase + _override_set_phase + _prev_block.

        UNKNOWN phase (inputs missing — typically <50ms post-restart) keeps
        persisted state intact; we don't overwrite target_dod or current_phase
        until phase computation has complete inputs.
        """
        new_phase = self._compute_phase(
            state,
            ems_interventions_blocked,
            start_charge_hour_override,
            sale_planned,
            reserve_floor,
        )

        if new_phase == Phase.UNKNOWN:
            return

        self.current_phase = new_phase

        # Override priority — record activation phase on first detection.
        if self._is_override_active(state, new_phase):
            override_value = state.dod_override
            assert override_value is not None  # _is_override_active guarantees
            self.target_dod = int(override_value)
            if self._override_set_phase is None:
                self._override_set_phase = new_phase
            return

        # Override expired or never active — clear tracker.
        self._override_set_phase = None

        if new_phase in DELEGATING_PHASES:
            block_fn = _PHASE_TO_BLOCK_FN[new_phase]
            new_block = block_fn(state, self._prev_block)
            self._prev_block = new_block
            self.target_dod = (
                0 if new_block else self._released_dod(new_phase, reserve_floor)
            )
        elif new_phase in DIRECT_PHASE_DOD:
            self.target_dod = DIRECT_PHASE_DOD[new_phase]
            # Sync hysteresis state to direct rule — when next delegating phase
            # arrives, hysteresis prev = current direct decision.
            self._prev_block = self.target_dod == 0
        else:
            self.target_dod = DEFAULT_DOD
            self._prev_block = False

    @staticmethod
    def _released_dod(phase: Phase, reserve_floor: int | None) -> int:
        """DoD when a delegating phase lets the house draw the battery."""
        if phase is Phase.RESERVE_FLOOR and reserve_floor is not None:
            return max(0, min(DEFAULT_DOD, 100 - reserve_floor))
        return DEFAULT_DOD

    def _is_override_active(self, state: InputState, current_phase: Phase) -> bool:
        """Check if user override applies right now.

        Active when input_number.ems_dod_override >= 0 AND current_phase matches
        the phase in which override was activated. On first detection (set_phase
        is None and value >= 0), update() records current phase as activation phase.
        """
        if state.dod_override is None or state.dod_override < 0:
            return False
        # First tick of override OR same phase as set → active.
        if self._override_set_phase is None:
            return True
        return current_phase == self._override_set_phase

    def _compute_phase(
        self,
        state: InputState,
        ems_interventions_blocked: bool = False,
        start_charge_hour_override: time | None = None,
        sale_planned: bool | None = None,
        reserve_floor: int | None = None,
    ) -> Phase:
        """Dispatch to phase by priority — first match wins.

        Priority: 1) interventions blocked (BatterySchedule active or user
        override), 2) time + flags → time-window phase. User override
        (input_number.ems_dod_override) wraps phase via update() — not part of
        this method's dispatch.
        """
        if ems_interventions_blocked:
            return Phase.INTERVENTIONS_BLOCKED

        if state.now is None:
            return Phase.UNKNOWN

        hour = state.now.hour
        if hour in PV_WINDOW_HOURS:
            return self._morning_phase(state, start_charge_hour_override)
        if hour >= PV_WINDOW_HOURS.stop:
            if state.is_workday is None:
                return Phase.UNKNOWN
            if state.is_workday and hour in evening_peak(state.now.date()):
                return Phase.EVENING_DISCHARGE
        return self._hold_phase(state, sale_planned, reserve_floor)

    @staticmethod
    def _morning_phase(
        state: InputState, start_charge_hour_override: time | None
    ) -> Phase:
        """Morning region 7:00..13:00 — weekend or workday."""
        assert state.now is not None
        if state.is_workday is None:
            return Phase.UNKNOWN
        if state.is_workday is False:
            return Phase.WEEKEND_MORNING
        # Workday morning — pre-charge or post-charge based on start_charge_hour
        if start_charge_hour_override is None:
            return Phase.UNKNOWN
        if state.now.time() < start_charge_hour_override:
            return Phase.WORKDAY_PRE_CHARGE
        return Phase.WORKDAY_POST_CHARGE

    @staticmethod
    def _hold_phase(
        state: InputState, sale_planned: bool | None, reserve_floor: int | None
    ) -> Phase:
        """13:00..07:00 outside a workday's T2 — hold for a sale, floor, or release.

        "The morning ahead" is tomorrow's before midnight and today's after it
        (`binary_sensor.workday` reflects the date that started at midnight —
        bug fixed 2026-05-29: reading `is_workday_tomorrow` after midnight
        pointed at day+2).

        `sale_planned` None (plans or prices not in yet, e.g. right after a
        restart) keeps the persisted phase rather than releasing the battery
        on partial inputs.
        """
        assert state.now is not None
        if sale_planned is None:
            return Phase.UNKNOWN
        if sale_planned:
            return Phase.HOLD_FOR_SALE
        hour = state.now.hour
        morning_is_workday = (
            state.is_workday_tomorrow
            if hour >= PV_WINDOW_HOURS.start
            else state.is_workday
        )
        if morning_is_workday is None:
            return Phase.UNKNOWN
        if morning_is_workday:
            if reserve_floor is not None:
                return Phase.RESERVE_FLOOR
            if hour >= T1SocReserve.TOMORROW_FROM_HOUR or hour < PV_WINDOW_HOURS.start:
                return Phase.NIGHT_PRESERVE
        return Phase.RELEASED

    # --- Persistence (cross HA restart) --- #

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_dod": self.target_dod,
            "current_phase": self.current_phase.value,
            "_override_set_phase": (
                self._override_set_phase.value if self._override_set_phase else None
            ),
            "_prev_block": self._prev_block,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DodPolicy:
        return cls(
            target_dod=int(data.get("target_dod", DEFAULT_DOD)),
            current_phase=_parse_phase(data.get("current_phase", Phase.UNKNOWN.value)),
            _override_set_phase=(
                _parse_phase(data["_override_set_phase"])
                if data.get("_override_set_phase")
                else None
            ),
            _prev_block=bool(data.get("_prev_block", False)),
        )


# Phases retired on 17.10.2026 (hold rule replaced the peak heuristic). A
# stored one restores as UNKNOWN; the next tick computes the phase afresh.
_RETIRED_PHASES: Final[frozenset[str]] = frozenset(
    {"afternoon_static", "afternoon_dynamic", "evening_preserve", "night_free"}
)


def _parse_phase(value: str) -> Phase:
    """Parse stored phase string. Migrates legacy values (Etap 0 rename)."""
    # Legacy storage: "ems_allow_discharge" → INTERVENTIONS_BLOCKED.
    if value == "ems_allow_discharge":
        return Phase.INTERVENTIONS_BLOCKED
    if value in _RETIRED_PHASES:
        return Phase.UNKNOWN
    return Phase(value)
