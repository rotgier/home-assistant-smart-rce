"""DoD Policy — single source of truth for inverter DoD register value.

Maps phase (time + flags) + BatteryManager hysteresis + user override → target_dod
(0..100). Replaces 4 fragile time-triggered HA automations:

- `Set SOC 90 at 7 and at 19`           → phases delegate to BatteryManager.block
- `Set Min SOC to 100 Afternoon`        → phase AFTERNOON_STATIC (direct 0)
- `Set Min SOC to 100 Evening`          → phase NIGHT_PRESERVE (direct 0)
- `ems-set-dod-from-block-discharge`    → replaced by DodPolicyActuator
                                          (driven adapter, ADR-019 pattern)

Self-healing on restart: each tick reads time + flags, computes phase from
scratch. No timing-sensitive trigger needed — DodPolicy is event-driven via
EMS update flow.

Compose-with-BatteryManager: phases WORKDAY_PRE_CHARGE / WORKDAY_POST_CHARGE /
AFTERNOON_DYNAMIC delegate DoD = `0 if battery.block else 90` (BatteryManager
keeps its hysteresis — DodPolicy just maps block→DoD). Other phases use
direct rules (fixed 0/90 based on time + RCE flags).

No entry-initial special case: BatteryManager hysteresis recomputes block on
every tick from instantaneous export + pv_5min state. At phase boundaries
(7:00 sharp, 13:00 sharp) the FIRST tick of new phase yields correct block
naturally — no need to override with arbitrary initial value.

Override: input_number.ems_dod_override ≥ 0 takes priority over phase logic.
Active until phase boundary — when current phase ≠ phase at override-set,
override expires and normal logic resumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from ..const import GROSS_MULTIPLIER

if TYPE_CHECKING:
    from .battery import BatteryManager
    from .discharge_slots import DischargeSlots
    from .input_state import InputState


class Phase(Enum):
    """DoD policy phases. Dispatched in priority order; first match wins."""

    OVERRIDE = "override"
    EMS_ALLOW_DISCHARGE = "ems_allow_discharge"
    WORKDAY_PRE_CHARGE = "workday_pre_charge"  # 7:00..start_charge, workday
    WORKDAY_POST_CHARGE = "workday_post_charge"  # start_charge..13:00, workday
    AFTERNOON_STATIC = "afternoon_static"  # 13:00..19:00, peak=True (any day)
    AFTERNOON_DYNAMIC = "afternoon_dynamic"  # 13:00..19:00, peak=False (any day)
    EVENING = "evening"  # 19:00..22:00 (any day)
    NIGHT_PRESERVE = "night_preserve"  # 22:00..07:00, preserve trigger
    NIGHT_FREE = "night_free"  # 22:00..07:00, no preserve
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
    Phase.EVENING: 90,  # 19:00..22:00 — allow evening discharge
    Phase.NIGHT_PRESERVE: 0,  # preserve for tomorrow's expensive morning
    Phase.NIGHT_FREE: 90,  # cheap morning ahead — battery free
    Phase.WEEKEND_MORNING: 0,  # weekend rano — passive PV capture
}

DEFAULT_DOD: int = 90  # safe fallback when inputs incomplete


@dataclass
class DodPolicy:
    """Computes target_dod per tick based on phase + battery + override.

    Persistent state (across HA restart):
    - target_dod: last emitted value (informational)
    - current_phase: phase computed at last tick (diagnostic + override expiry)
    - _override_set_phase: phase in which override was activated (for expiry)
    """

    target_dod: int = DEFAULT_DOD
    current_phase: Phase = Phase.UNKNOWN
    _override_set_phase: Phase | None = None

    def update(
        self,
        state: InputState,
        battery_mgr: BatteryManager,
        discharge_slots: DischargeSlots,
    ) -> None:
        """Compute target_dod for this tick.

        Reads InputState (time, peak, override) + BatteryManager.block +
        DischargeSlots (best_morning_discharge_slot for night-preserve dispatch).
        Mutates self.target_dod + self.current_phase + self._override_set_phase.
        """
        new_phase = self._compute_phase(state, discharge_slots)
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

        # Delegate or direct rule. BatteryManager hysteresis recomputes on every
        # tick (including phase transition tick) — yields correct block naturally,
        # no entry-initial special case needed.
        if new_phase in DELEGATING_PHASES:
            self.target_dod = 0 if battery_mgr.should_block_battery_discharge else 90
        elif new_phase in DIRECT_PHASE_DOD:
            self.target_dod = DIRECT_PHASE_DOD[new_phase]
        else:
            self.target_dod = DEFAULT_DOD

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
        self, state: InputState, discharge_slots: DischargeSlots
    ) -> Phase:
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
            return self._night_phase(state, discharge_slots)

        # Evening 19:00..22:00 — allow discharge regardless of weekday/peak
        if 19 <= hour < 22:
            return Phase.EVENING

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
    def _night_phase(state: InputState, discharge_slots: DischargeSlots) -> Phase:
        """22:00..07:00 — preserve (workday tomorrow OR expensive morning) or free.

        Morning discharge price comes from `discharge_slots.best_morning_discharge_slot`
        (smart_rce-computed, netto) × GROSS_MULTIPLIER → gross gr/kWh, compared
        with `input_number.rce_high_price_threshold_gross` (user UI).
        """
        if state.is_workday_tomorrow is True:
            return Phase.NIGHT_PRESERVE
        slot = discharge_slots.best_morning_discharge_slot
        if slot is not None and state.rce_high_price_threshold_gross is not None:
            morning_gross = slot.price * GROSS_MULTIPLIER
            if morning_gross > state.rce_high_price_threshold_gross:
                return Phase.NIGHT_PRESERVE
        return Phase.NIGHT_FREE

    # --- Persistence (cross HA restart) --- #

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_dod": self.target_dod,
            "current_phase": self.current_phase.value,
            "_override_set_phase": (
                self._override_set_phase.value if self._override_set_phase else None
            ),
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
        )
