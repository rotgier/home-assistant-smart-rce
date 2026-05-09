"""DoD Policy — single source of truth for inverter DoD register value.

Maps phase (time + flags) + BatteryManager hysteresis + user override → target_dod
(0..100). Replaces 4 fragile time-triggered HA automations:

- `Set SOC 90 at 7 and at 19`           → phases WORKDAY_PRE_CHARGE entry + EVENING
- `Set Min SOC to 100 Afternoon`        → phase AFTERNOON_STATIC (direct 0)
- `Set Min SOC to 100 Evening`          → phase NIGHT_PRESERVE (direct 0)
- `ems-set-dod-from-block-discharge`    → replaced by `ems-set-dod-from-target-dod`
                                          (event-driven sensor → number copy)

Self-healing on restart: each tick reads time + flags, computes phase from
scratch. No timing-sensitive trigger is needed — phase entry initial DoD is
emitted only when the computed phase differs from persisted `_last_phase`.

Compose-with-BatteryManager: phases WORKDAY_PRE_CHARGE / WORKDAY_POST_CHARGE /
AFTERNOON_DYNAMIC delegate DoD = `0 if battery.block else 90` (BatteryManager
keeps its hysteresis — DodPolicy just maps block→DoD). Other phases use
direct rules (fixed 0/90 based on time + RCE flags).

Phase entry initial DoD (only PRE_CHARGE @ 07:00 and AFTERNOON_DYNAMIC @ 13:00):
emitted on first tick of new phase to avoid jarring DoD jump from
BatteryManager's stale state. Subsequent ticks delegate normally.

Override: input_number.ems_dod_override ≥ 0 takes priority. Active until
phase boundary — when current phase ≠ phase at override-set, override expires
and normal logic resumes (with new phase's entry initial if applicable).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .battery import BatteryManager
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

# Phase entry initial DoD — emitted on first tick of new phase only when
# transition timing matters (replaces 'Set SOC X at HH' YAML automations).
# Phases not in this dict: no initial, immediate delegate / direct rule.
PHASE_ENTRY_INITIAL_DOD: dict[Phase, int] = {
    # mimics 'Set SOC 90 at 7' — allow morning discharge gambit at 07:00.
    # BatteryManager hysteresis takes over from tick 2 (block=True if export
    # > 100 Wh sustained → DoD=0).
    Phase.WORKDAY_PRE_CHARGE: 90,
    # 13:00 transition — preserve battery (DoD=0) regardless of static/dynamic
    # variant. Static stays at 0 (direct rule); dynamic gives BatteryManager
    # hysteresis room to release to 90 if hourly export < 0 (deficit). Safer
    # default than 90 — preserves capacity for evening peak even when peak
    # flag is False but afternoon develops actual peak signal later.
    Phase.AFTERNOON_DYNAMIC: 0,
    # WORKDAY_POST_CHARGE: NO entry initial. BatteryManager's first tick
    # cleanly catches up via hysteresis (no jarring transition).
}

# Phases with direct (non-delegating, non-initial) DoD rule.
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
    - target_dod: last emitted value
    - _last_phase: detect phase transitions for entry initial DoD
    - _override_set_phase: phase in which override was activated (for expiry)
    """

    target_dod: int = DEFAULT_DOD
    _last_phase: Phase = Phase.UNKNOWN
    _override_set_phase: Phase | None = None

    def update(self, state: InputState, battery_mgr: BatteryManager) -> None:
        """Compute target_dod for this tick.

        Reads InputState (time, peak, override, etc.) + BatteryManager.block.
        Mutates self.target_dod and self._last_phase.
        """
        new_phase = self._compute_phase(state)

        # Override path takes priority over phase entry / delegation.
        if self._is_override_active(state, new_phase):
            override_value = state.dod_override
            assert override_value is not None  # _is_override_active guarantees
            self.target_dod = int(override_value)
            if self._override_set_phase is None:
                self._override_set_phase = new_phase
            self._last_phase = new_phase
            return

        # Override expired or never active — clear tracker.
        self._override_set_phase = None

        # Phase transition — emit entry initial if defined for the new phase.
        if new_phase != self._last_phase:
            initial = PHASE_ENTRY_INITIAL_DOD.get(new_phase)
            if initial is not None:
                self.target_dod = initial
                self._last_phase = new_phase
                return

        # Same phase OR phase without entry initial → delegate or direct rule.
        if new_phase in DELEGATING_PHASES:
            self.target_dod = 0 if battery_mgr.should_block_battery_discharge else 90
        elif new_phase in DIRECT_PHASE_DOD:
            self.target_dod = DIRECT_PHASE_DOD[new_phase]
        else:
            self.target_dod = DEFAULT_DOD

        self._last_phase = new_phase

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
    def _night_phase(state: InputState) -> Phase:
        """22:00..07:00 — preserve (workday tomorrow OR expensive morning) or free."""
        if state.is_workday_tomorrow is True:
            return Phase.NIGHT_PRESERVE
        if (
            state.rce_morning_discharge_price is not None
            and state.rce_high_price_threshold_gross is not None
            and state.rce_morning_discharge_price > state.rce_high_price_threshold_gross
        ):
            return Phase.NIGHT_PRESERVE
        return Phase.NIGHT_FREE

    # --- Persistence (cross HA restart) --- #

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_dod": self.target_dod,
            "_last_phase": self._last_phase.value,
            "_override_set_phase": (
                self._override_set_phase.value if self._override_set_phase else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DodPolicy:
        return cls(
            target_dod=int(data.get("target_dod", DEFAULT_DOD)),
            _last_phase=Phase(data.get("_last_phase", Phase.UNKNOWN.value)),
            _override_set_phase=(
                Phase(data["_override_set_phase"])
                if data.get("_override_set_phase")
                else None
            ),
        )
