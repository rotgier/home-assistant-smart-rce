"""NegativeIntervention — active NEGATIVE intervention session.

Activated by manager when hourly balance is negative (net import). Replaces
YAML automation `Inverter grid export to avoid NEGATIVE balance` (Sell Power
1500W) — Sell Power did not account for grid side load (observed dishwasher
1.5kW + Sell Power 1500W = actual export 100-200W instead of 1500W).

Strategy: 10 buckets on `pv_available` (= PV − house_without_heaters). Each
bucket maps pv_available to signed xset:
- xset > 0  → CHARGE_BATTERY (PV surplus, charge battery, export ~1500W)
- xset = 0  → DISCHARGE_BATTERY xset=0 (bucket STOP, battery idle, export = pv_avail)
- xset < 0  → DISCHARGE_BATTERY (PV deficit, draw from battery, export ~1500W)

Hysteresis ±300W on bucket boundaries (analog to POSITIVE). SoC clamp:
- DISCHARGE bucket requires SoC > min_soc (= 100 - DoD)
- CHARGE bucket + SoC=100 → clamp to bucket STOP (PV export offsets NEGATIVE)
- bucket STOP always feasible

Pre_charge window does NOT block (POSITIVE skips, NEGATIVE allowed — battery
can discharge if SoC > min).

Target meter (charge_limit > 7, reserved=5500W in water_heater for NEGATIVE,
heater practically always OFF because heater_budget = pv_avail − 5500 is
negative in typical range) — meter = pv_avail − heaters − battery_actual:

    ┌─────────────────┬──────────┬─────────┬──────────────────────────┐
    │ Active pv_avail │ xset_sig │ battery │ meter (heater OFF)       │
    ├─────────────────┼──────────┼─────────┼──────────────────────────┤
    │ > 5000          │ +4000    │  +4000  │ ≥ +1000 (at pv ≥ 5000)   │
    │ 4000–5000       │ +3000    │  +3000  │   +1500                  │
    │ 3000–4000       │ +2000    │  +2000  │   +1500                  │
    │ 2000–3000       │ +1000    │  +1000  │   +1500                  │
    │ 1000–2000       │      0   │      0  │   +1500 (= pv_avail)     │
    │ 0–1000          │  −1000   │  −1000  │   +1500                  │
    │ −1000–0         │  −2000   │  −2000  │   +1500                  │
    │ −2000–−1000     │  −3000   │  −3000  │   +1500                  │
    │ −3000–−2000     │  −4000   │  −4000  │   +1500                  │
    │ −4000–−3000     │  −6000   │  −5300* │ ≈ +1800 (BMS clamp)      │
    │ ≤ −4000         │  −6000   │  −5300* │ < +1500 (deep deficit)   │
    └─────────────────┴──────────┴─────────┴──────────────────────────┘

    * BMS clamp ~5300W at max discharge (target −6000 unreachable —
      typically practical ~5000-5300W).

    Formula: xset_signed = bucket_center − 1500, so
             meter = center − xset_signed = +1500 fixed (outside top/bottom).

Cross-cutting checks (manager handles, NOT in try_enter / continue_or_exit):
- ems_allow_discharge_override (global block)
- balance range (manager routes by `entry_threshold(state)`)
- too_late_in_hour entry block (manager: now ≥ XX:59:40)
- other_ems_automation_active_this_hour (manager)
- hour_rollover (manager: started_hour mismatch)
- end_of_hour_cleanup exit (manager: now ≥ XX:59:50)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Final

from custom_components.smart_rce.domain.grid_export.intervention import (
    CONTINUE,
    ContinueResult,
    EntryResult,
    InterventionDirection,
)
from custom_components.smart_rce.domain.input_state import InputState

# Mode constants (Goodwe EMS)
_CHARGE_MODE: Final[str] = "charge_battery"
_STANDBY_MODE: Final[str] = "discharge_battery"  # bucket STOP (xset=0)
_DISCHARGE_MODE: Final[str] = "discharge_battery"  # bucket DISCHARGE (xset>0)

# Entry threshold dynamic — pre-45min: -0.05 (tolerate moderate negative,
# time for natural recovery from PV); post-45min: 0.0 (any negative — hour
# is ending).
ENTRY_THRESHOLD_EARLY_KWH: Final[float] = -0.05
ENTRY_THRESHOLD_LATE_KWH: Final[float] = 0.0
EXIT_BALANCE_KWH: Final[float] = 0.0
LATE_HALF_HOUR_MINUTE: Final[int] = 45

# SoC floors / ceilings
SOC_HARD_FLOOR: Final[int] = 10
SOC_CEILING: Final[int] = 100  # bucket charge clamp (= battery full)

# Hysteresis for bucket transitions
HYSTERESIS_W: Final[int] = 300

# Adaptive buckets — `(lower, upper, xset_signed)`.
# Activates when `lower < pv_available <= upper` (top bucket: upper=None=+inf).
# Bucket center yields export ~1500W (Xset = lower - 1000).
_ADAPTIVE_BUCKETS: Final[tuple[tuple[int, int | None, int], ...]] = (
    (5000, None, 4000),  # > 5000 W → charge 4000 (export ≥ 1000)
    (4000, 5000, 3000),  # charge 3000 (export ~1500)
    (3000, 4000, 2000),
    (2000, 3000, 1000),
    (1000, 2000, 0),  # bucket STOP — battery idle, export = pv_avail
    (0, 1000, -1000),  # discharge 1000 (export ~1500)
    (-1000, 0, -2000),
    (-2000, -1000, -3000),
    (-3000, -2000, -4000),
    (-4000, -3000, -6000),  # discharge 6000 (BMS max ~5.2-5.3 kW)
    # pv_avail ≤ -4000: fallback to cap (-6000)
)


def entry_threshold(state: InputState) -> float:
    """Time-dependent entry threshold (-0.05 pre-45min, 0.0 post-45min).

    Public — manager calls before `NegativeIntervention.try_enter` so that
    routing balance < threshold uses the dynamic threshold. Module-level
    because used from another module (manager) — not a class helper.
    """
    if state.now.minute < LATE_HALF_HOUR_MINUTE:
        return ENTRY_THRESHOLD_EARLY_KWH
    return ENTRY_THRESHOLD_LATE_KWH


@dataclass
class NegativeIntervention:
    """Active NEGATIVE intervention session.

    Mutable entity — `_continue` mutates recommended_*/last_reason via
    `_commit` helper. Manager does not create new instances per tick.
    """

    direction: ClassVar[InterventionDirection] = InterventionDirection.NEGATIVE

    started_hour: int
    recommended_mode: str = _STANDBY_MODE
    recommended_xset: int = 0
    last_reason: str = ""
    _xset_signed: int | None = field(default=None, repr=False)
    """Signed bucket value (positive=charge, negative=discharge, 0=STOP).
    None initially → fresh lookup (no hysteresis on first _continue)."""

    @classmethod
    def try_enter(cls, state: InputState) -> EntryResult:
        """Try to enter NEGATIVE intervention.

        Caller (GridExportManager) MUST verify global guards first:
        - balance < entry_threshold(state) (range routing)
        - no ems_allow_discharge_override
        - not too_late_in_hour
        - not other_ems_automation_active_this_hour

        Checks intervention-specific gates (SoC, DoD, pv_available) plus
        entry feasibility (bucket discharge requires SoC > min_soc), then
        delegates to `_enter` for instance creation + initial _continue.
        """
        if state.battery_soc <= SOC_HARD_FLOOR:
            return EntryResult.blocked("soc_below_hard_floor")
        if state.depth_of_discharge is None:
            return EntryResult.blocked("none_depth_of_discharge")
        if state.pv_available is None:
            return EntryResult.blocked("none_pv_available")
        # Entry feasibility — discharge bucket requires SoC > min_soc.
        # Charge bucket at SoC=100 does NOT block (clamp to bucket STOP in _continue).
        if cls._lookup_xset(state.pv_available) < 0 and state.battery_soc <= (
            100 - state.depth_of_discharge
        ):
            return EntryResult.blocked("soc_at_dod_floor_no_discharge")
        return cls._enter(state)

    @classmethod
    def _enter(cls, state: InputState) -> EntryResult:
        """Create blank intervention, run initial _continue, map to EntryResult.

        Initial _xset_signed=None default → fresh lookup (no hysteresis on entry).
        """
        intervention = cls(started_hour=state.now.hour)
        result = intervention._continue(state)  # noqa: SLF001 — same-class access
        if result.is_exit:
            return EntryResult.blocked(result.exit_reason)
        return EntryResult.entered(intervention)

    def continue_or_exit(self, state: InputState) -> ContinueResult:
        """Continue NEGATIVE intervention. Mutates self in-place on continue.

        Caller (GridExportManager) MUST verify global guards first:
        - hour_rollover (started_hour mismatch)
        - end_of_hour_cleanup (now ≥ XX:59:50)
        - no ems_allow_discharge_override
        """
        if state.exported_energy_hourly > EXIT_BALANCE_KWH:
            return ContinueResult.exit_with("negative_balance_recovered")
        if state.depth_of_discharge is None:
            return ContinueResult.exit_with("none_depth_of_discharge_exit")
        if state.pv_available is None:
            return ContinueResult.exit_with("none_pv_available")
        return self._continue(state)

    def _continue(self, state: InputState) -> ContinueResult:
        """Compute new decision and commit to self. Returns CONTINUE or exit.

        Used by both _enter (initial fresh lookup) and continue_or_exit
        (hysteresis-aware). Pre-conditions verified by caller — pv_available
        and depth_of_discharge are not None.

        Flow: hysteresis lookup → SoC clamp → post-clamp DoD check → commit.
        """
        xset_signed, is_stay = self._resolve_xset_with_hysteresis(state.pv_available)
        xset_signed, is_stay = self._clamp_charge_bucket(xset_signed, is_stay, state)
        # Post-clamp SoC floor check — discharge bucket requires SoC > min_soc.
        if xset_signed < 0 and state.battery_soc <= (100 - state.depth_of_discharge):
            return ContinueResult.exit_with("soc_at_dod_floor_exit")
        return self._commit(xset_signed, is_stay, state.pv_available)

    def _resolve_xset_with_hysteresis(self, pv_available: float) -> tuple[int, bool]:
        """Lookup with hysteresis (current bucket + ±300W tolerance).

        Returns (xset_signed, is_stay):
        - is_stay=True when hysteresis kept current bucket
        - is_stay=False when fresh lookup (bucket changed, or current outside buckets)
        """
        current_range = self._xset_range(self._xset_signed)
        if current_range is not None:
            lower, upper = current_range
            if (lower - HYSTERESIS_W) < pv_available <= (upper + HYSTERESIS_W):
                return self._xset_signed, True  # type: ignore[return-value]
        return self._lookup_xset(pv_available), False

    @staticmethod
    def _xset_range(xset_signed: int | None) -> tuple[float, float] | None:
        """Range of pv_available that would activate given xset_signed.

        Returns (lower, upper) or None when xset_signed is not in buckets.
        Top bucket has upper=inf.
        """
        if xset_signed is None:
            return None
        for lower, upper, xs in _ADAPTIVE_BUCKETS:
            if xs == xset_signed:
                upper_f = float("inf") if upper is None else float(upper)
                return (float(lower), upper_f)
        return None

    @staticmethod
    def _lookup_xset(pv_available: float) -> int:
        """Find xset_signed for pv_available in _ADAPTIVE_BUCKETS.

        Multi-caller helper (try_enter feasibility + _resolve_xset_with_hysteresis).
        Fallback: cap at deepest bucket (pv_avail ≤ -4000) → -6000.
        """
        for lower, upper, xset_signed in _ADAPTIVE_BUCKETS:
            if upper is None:
                if pv_available > lower:
                    return xset_signed
            elif lower < pv_available <= upper:
                return xset_signed
        return _ADAPTIVE_BUCKETS[-1][2]

    @staticmethod
    def _clamp_charge_bucket(
        xset_signed: int, is_stay: bool, state: InputState
    ) -> tuple[int, bool]:
        """Clamp charge bucket (xset>0) to bucket STOP when battery full or toggle off.

        - SoC = 100 → battery full, cannot charge, but PV export offsets NEGATIVE
          (bucket STOP yields pv_avail export).
        - battery_charge_toggle_on = False → user disabled charging, manager
          respects (bucket STOP). NEGATIVE branch still active — pv_avail
          export saves the balance.
        """
        if xset_signed <= 0:
            return xset_signed, is_stay
        if state.battery_soc is not None and state.battery_soc >= SOC_CEILING:
            return 0, False
        if state.battery_charge_toggle_on is False:
            return 0, False
        return xset_signed, is_stay

    def _commit(self, xset_signed: int, is_stay: bool, pv: float) -> ContinueResult:
        """Translate signed bucket value → (mode, xset, reason) and mutate self.

        - xset_signed > 0 → charge_battery with xset = xset_signed
        - xset_signed = 0 → discharge_battery with xset = 0 (bucket STOP)
        - xset_signed < 0 → discharge_battery with xset = abs(xset_signed)
        """
        prefix = "negative_stay" if is_stay else "negative"
        pv_int = int(pv)
        self._xset_signed = xset_signed
        if xset_signed > 0:
            self.recommended_mode = _CHARGE_MODE
            self.recommended_xset = xset_signed
            self.last_reason = f"{prefix}_charge_{xset_signed}W_pv_avail_{pv_int}"
        elif xset_signed == 0:
            self.recommended_mode = _STANDBY_MODE
            self.recommended_xset = 0
            self.last_reason = f"{prefix}_stop_xset_0_pv_avail_{pv_int}"
        else:
            self.recommended_mode = _DISCHARGE_MODE
            self.recommended_xset = abs(xset_signed)
            self.last_reason = (
                f"{prefix}_discharge_{abs(xset_signed)}W_pv_avail_{pv_int}"
            )
        return CONTINUE
