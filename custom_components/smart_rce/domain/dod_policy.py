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
- **EMS_ALLOW_DISCHARGE** (`ems_allow_discharge_override=True`): smart_rce
  "stays out of the way" → DoD=90, lets other automations rule.
- **WORKDAY_PRE_CHARGE** (7:00 → start_charge_hour, workday): delegates to
  `block_pre_charge` — hysteresis 100/50 Wh + instant_surplus extension.
- **WORKDAY_POST_CHARGE** (start_charge_hour → 13:00, workday): delegates
  to `block_post_charge` — dual-trigger (instant + hourly). Two-tier defense
  with POSITIVE intervention (POSITIVE first line >60 Wh, block_post_charge
  second line >=100 Wh / instant_surplus when POSITIVE cannot absorb, e.g.
  SoC=100% / BMS clamp / sustained surplus).
- **AFTERNOON_STATIC** (13:00 → 19:00, peak=True): direct DoD=0 — preserve
  for evening peak. Battery typically full → POSITIVE entry blocked by
  `soc_at_entry_ceiling` anyway.
- **AFTERNOON_DYNAMIC** (13:00 → 19:00, peak=False): delegates to
  `block_afternoon_dynamic` — aggressive thresholds (>0 Wh hourly) since
  past PV peak.
- **EVENING_DISCHARGE** (19:00 → 22:00, workday today OR weekend without
  preserve): direct DoD=90. Workday: cover expensive evening consumption;
  explicit discharge automations use `ems_allow_discharge_override` for fast
  discharge windows. Weekend: free when no peak ahead and tomorrow=weekend.
- **EVENING_PRESERVE** (19:00 → 22:00, weekend today AND (peak ahead OR
  workday tomorrow)): direct DoD=0 — protect battery for upcoming load.
- **NIGHT_PRESERVE** (22:00 → 07:00, workday tomorrow): direct DoD=0 —
  preserve for tomorrow morning load.
- **NIGHT_FREE** (22:00 → 07:00, weekend tomorrow): direct DoD=90 — free
  discharge.
- **WEEKEND_MORNING** (7:00 → 13:00, weekend): direct DoD=0 — passive PV
  capture (RCE typically flat, no expensive hours to protect surplus).
- **UNKNOWN** (inputs missing): keep persisted state.

Coordination with GridExportManager (POSITIVE / NEGATIVE intervention):

    | Phase              | block_discharge       | POSITIVE         | NEGATIVE |
    |--------------------|-----------------------|------------------|----------|
    | EMS_ALLOW          | False (always)        | exits            | exits    |
    | WORKDAY_PRE        | hysteresis            | blocked          | yes      |
    | WORKDAY_POST       | dual-trigger          | yes (1st line)   | yes      |
    | AFTERNOON_STATIC   | False (DoD=0 direct)  | rare (SoC ceil)  | yes      |
    | AFTERNOON_DYNAMIC  | dual-trigger          | yes (1st line)   | yes      |
    | EVENING_DISCHARGE  | False (DoD=90 direct) | rare             | yes      |
    | EVENING_PRESERVE   | False (DoD=0 direct)  | rare             | yes      |
    | NIGHT_PRESERVE     | False (DoD=0 direct)  | rare             | yes      |
    | NIGHT_FREE         | False (DoD=90 direct) | rare             | yes      |
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
survives HA restart via `dod_policy_persistence`.

See `context/target_soc_algorithm.md` for broader context.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from .block_discharge import (
    block_afternoon_dynamic,
    block_post_charge,
    block_pre_charge,
)

if TYPE_CHECKING:
    from .input_state import InputState


class Phase(Enum):
    """DoD policy phases. Dispatched in priority order; first match wins."""

    OVERRIDE = "override"
    EMS_ALLOW_DISCHARGE = "ems_allow_discharge"
    WORKDAY_PRE_CHARGE = "workday_pre_charge"  # 7:00..start_charge, workday
    WORKDAY_POST_CHARGE = "workday_post_charge"  # start_charge..13:00, workday
    AFTERNOON_STATIC = "afternoon_static"  # 13:00..19:00, peak=True (any day)
    AFTERNOON_DYNAMIC = "afternoon_dynamic"  # 13:00..19:00, peak=False (any day)
    EVENING_DISCHARGE = "evening_discharge"  # 19:00..22:00, workday OR weekend free
    EVENING_PRESERVE = "evening_preserve"  # 19:00..22:00, weekend with peak/preserve
    NIGHT_PRESERVE = "night_preserve"  # 22:00..07:00, workday tomorrow
    NIGHT_FREE = "night_free"  # 22:00..07:00, weekend tomorrow
    WEEKEND_MORNING = "weekend_morning"  # 7:00..13:00, weekend
    UNKNOWN = "unknown"  # fallback (missing inputs)


# Phases where DodPolicy delegates DoD to BatteryManager.block_discharge
# (block=True → DoD=0, block=False → DoD=90).
DELEGATING_PHASES: frozenset[Phase] = frozenset(
    {
        Phase.WORKDAY_PRE_CHARGE,
        Phase.WORKDAY_POST_CHARGE,
        Phase.AFTERNOON_DYNAMIC,
    }
)

# Phases with direct (non-delegating) DoD rule.
DIRECT_PHASE_DOD: dict[Phase, int] = {
    Phase.EMS_ALLOW_DISCHARGE: 90,  # smart_rce off — let other automations rule
    Phase.AFTERNOON_STATIC: 0,  # peak today — preserve for evening
    Phase.EVENING_DISCHARGE: 90,  # workday evening (cover peak) OR weekend free
    Phase.EVENING_PRESERVE: 0,  # weekend evening — peak ahead OR workday tomorrow
    Phase.NIGHT_PRESERVE: 0,  # workday tomorrow — preserve for morning load
    Phase.NIGHT_FREE: 90,  # weekend tomorrow — battery free
    Phase.WEEKEND_MORNING: 0,  # weekend rano — passive PV capture
}

DEFAULT_DOD: int = 90  # safe fallback when inputs incomplete

# Mapping delegating phase → stateless hysteresis function.
_PHASE_TO_BLOCK_FN = {
    Phase.WORKDAY_PRE_CHARGE: block_pre_charge,
    Phase.WORKDAY_POST_CHARGE: block_post_charge,
    Phase.AFTERNOON_DYNAMIC: block_afternoon_dynamic,
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

    def update(self, state: InputState) -> None:
        """Compute target_dod for this tick.

        Reads InputState (time, peak, override, exported_energy, pv_5min,
        is_workday, is_workday_tomorrow). Mutates target_dod + current_phase
        + _override_set_phase + _prev_block.

        UNKNOWN phase (inputs missing — typically <50ms post-restart) keeps
        persisted state intact; we don't overwrite target_dod or current_phase
        until phase computation has complete inputs.
        """
        new_phase = self._compute_phase(state)

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
            self.target_dod = 0 if new_block else 90
        elif new_phase in DIRECT_PHASE_DOD:
            self.target_dod = DIRECT_PHASE_DOD[new_phase]
            # Sync hysteresis state to direct rule — when next delegating phase
            # arrives, hysteresis prev = current direct decision.
            self._prev_block = self.target_dod == 0
        else:
            self.target_dod = DEFAULT_DOD
            self._prev_block = False

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

    def _compute_phase(self, state: InputState) -> Phase:
        """Dispatch to phase by priority — first match wins.

        Priority: 1) EMS allow discharge override (smart_rce off), 2) time +
        flags → time-window phase. User override (input_number.ems_dod_override)
        wraps phase via update() — not part of this method's dispatch.
        """
        if state.ems_allow_discharge_override is True:
            return Phase.EMS_ALLOW_DISCHARGE

        if state.now is None:
            return Phase.UNKNOWN

        hour = state.now.hour

        # Night phases 22:00..07:00 (next day)
        if hour >= 22 or hour < 7:
            return self._night_phase(state)

        # Evening 19:00..22:00 — workday vs weekend distinction
        if 19 <= hour < 22:
            return self._evening_phase(state)

        # Afternoon 13:00..19:00 — peak preserve OR dynamic hysteresis
        if 13 <= hour < 19:
            if state.rce_should_hold_for_peak is True:
                return Phase.AFTERNOON_STATIC
            return Phase.AFTERNOON_DYNAMIC

        # Morning region 7:00..13:00 — weekend or workday
        if state.is_workday is None:
            return Phase.UNKNOWN
        if state.is_workday is False:
            return Phase.WEEKEND_MORNING

        # Workday morning — pre-charge or post-charge based on start_charge_hour
        if state.start_charge_hour_override is None:
            return Phase.UNKNOWN
        if state.now.time() < state.start_charge_hour_override:
            return Phase.WORKDAY_PRE_CHARGE
        return Phase.WORKDAY_POST_CHARGE

    @staticmethod
    def _evening_phase(state: InputState) -> Phase:
        """19:00..22:00 — workday discharge OR weekend preserve/free.

        - Workday today → DISCHARGE (DoD=90): cover expensive evening peak load.
          Other automations (`Battery Discharge in the evening`) flip
          `ems_allow_discharge_override` for explicit fast discharge windows;
          when off, smart_rce keeps DoD=90 so battery can serve consumption.
        - Weekend today + hold_for_peak=True → PRESERVE (DoD=0): peak ahead
          (today evening or tomorrow morning).
        - Weekend today + workday_tomorrow=True → PRESERVE: morning load ahead.
        - Weekend today + weekend_tomorrow + no peak → DISCHARGE.
        """
        if state.is_workday is None:
            return Phase.UNKNOWN
        if state.is_workday is True:
            return Phase.EVENING_DISCHARGE
        # Weekend today
        if state.rce_should_hold_for_peak is True:
            return Phase.EVENING_PRESERVE
        if state.is_workday_tomorrow is True:
            return Phase.EVENING_PRESERVE
        if state.is_workday_tomorrow is None:
            return Phase.UNKNOWN
        return Phase.EVENING_DISCHARGE

    @staticmethod
    def _night_phase(state: InputState) -> Phase:
        """22:00..07:00 — preserve when workday tomorrow, else free.

        Simplified rule: only `is_workday_tomorrow` decides. Morning RCE-price
        check (`discharge_slots.best_morning_discharge_slot`) intentionally
        dropped — that slot is computed for emergency morning discharge only
        (5:00..7:00 window) and not reliable as preservation trigger.
        """
        if state.is_workday_tomorrow is True:
            return Phase.NIGHT_PRESERVE
        if state.is_workday_tomorrow is None:
            return Phase.UNKNOWN
        return Phase.NIGHT_FREE

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
            current_phase=Phase(data.get("current_phase", Phase.UNKNOWN.value)),
            _override_set_phase=(
                Phase(data["_override_set_phase"])
                if data.get("_override_set_phase")
                else None
            ),
            _prev_block=bool(data.get("_prev_block", False)),
        )
