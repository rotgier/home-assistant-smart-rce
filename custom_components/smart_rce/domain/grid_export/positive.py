"""PositiveIntervention — active POSITIVE intervention session.

Activated by manager when hourly balance is excessively positive (export
> 0.06 kWh) — manager forces CHARGE_BATTERY to consume the balance (instead
of feeding it back to the grid).

Internal strategies:
- STANDBY (charge_battery xset=0) — when pv_power_avg_2_minutes < 200W
  (night, battery target=0, house from grid — consumes POSITIVE balance).
  Empirically verified: DISCHARGE_BATTERY xset=0 is silently ignored when
  DoD=0; CHARGE_BATTERY xset=0 reliably parks the battery at zero power.
- charge_adaptive — Xset from lookup on `state.pv_available`. 6 buckets
  (threshold → Xset). Lower threshold pv_available > -1000 → AUTO but stay
  in intervention (block_discharge in battery.py takes over when hourly
  goes negative).

Low BMS shortcut — when battery_charge_limit ≤ 7A, BMS clamps to ~2 kW so
the lookup is irrelevant. Fixed Xset 3500W (+1.5 kW margin, BMS will limit
anyway).

Hysteresis ±300W on bucket boundaries — eliminates flapping when pv_available
oscillates near the boundary.

Pre_charge window blocks POSITIVE entry/continue (BatteryManager rules via
block_discharge hysteresis). NEGATIVE works in pre_charge (separate strategy).

Target meter (charge_limit > 7, reserved=3500W in water_heater, battery cap
5300W at xset=6000) — meter = pv_avail − heaters − battery_actual. Top
bucket xset=6000 split into 4 sub-ranges by water_heater thresholds
(heater_budget = pv_avail - 3500, SMALL≥1500, BIG≥3000, BOTH≥4500):

    ┌─────────────────┬──────┬─────────┬────────┬────────────┬──────────┐
    │ Active pv_avail │ xset │ battery │ center │ heater     │ meter    │
    ├─────────────────┼──────┼─────────┼────────┼────────────┼──────────┤
    │ 8000–9000       │ 6000 │  5300   │ 8500   │ BOTH 4.5k  │ ≈ -1300  │
    │ 6500–8000       │ 6000 │  5300   │ 7250   │ BIG 3k     │ ≈ -1050  │
    │ 5000–6500       │ 6000 │  5300   │ 5750   │ SMALL 1.5k │ ≈ -1050  │
    │ 4000–5000       │ 6000 │  5300   │ 4500   │ OFF        │  ≈ -800  │
    │ 3000–4000       │ 5000 │  5000   │ 3500   │ OFF        │   -1500  │
    │ 2000–3000       │ 4000 │  4000   │ 2500   │ OFF        │   -1500  │
    │ 1000–2000       │ 3000 │  3000   │ 1500   │ OFF        │   -1500  │
    │ 0–1000          │ 2000 │  2000   │  500   │ OFF        │   -1500  │
    │ -1000–0         │ 1000 │  1000   │ -500   │ OFF        │   -1500  │
    │ ≤ -1000         │ AUTO │    0    │   —    │     —      │    —     │
    └─────────────────┴──────┴─────────┴────────┴────────────┴──────────┘

    Lower buckets (xset≤5000): meter = center - xset = -1500 fixed.
    Top bucket (xset=6000, battery cap 5300W): meter depends on heaters
    enabled by water_heater — average ≈ -1000W (1 kW import).
    ≤ -1000: AUTO mode, block_discharge in battery.py takes over.

Cross-cutting checks (manager handles, NOT in try_enter / continue_or_exit):
- ems_interventions_blocked (global block)
- balance range (manager routes by `BALANCE_GATE_KWH`)
- too_late_in_hour entry block (manager: now ≥ XX:59:40)
- other_ems_automation_active_this_hour (manager)
- hour_rollover (manager: started_hour mismatch)
- end_of_hour_cleanup exit (manager: now ≥ XX:59:50)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Final

if TYPE_CHECKING:
    from datetime import time

from custom_components.smart_rce.domain.grid_export.intervention import (
    CONTINUE,
    ContinueResult,
    EntryResult,
    InterventionDirection,
)
from custom_components.smart_rce.domain.input_state import InputState

# Mode constants (Goodwe EMS).
#
# STANDBY uses CHARGE_BATTERY (not DISCHARGE_BATTERY) because empirically
# DISCHARGE_BATTERY xset=0 is silently ignored by the inverter when DoD=0
# (PV surplus still charges the battery). CHARGE_BATTERY xset=0 reliably
# parks the battery at zero power regardless of DoD, AND also works when
# battery_charge_allowed is False — despite the misleading "CHARGE" label,
# with xset=0 the inverter does not actually charge, just enforces zero
# net battery power. Same string as _CHARGE_MODE — kept as a separate
# constant to preserve conceptual naming (STANDBY vs CHARGE differ by xset
# value, not by EMS mode).
_AUTO_MODE: Final[str] = "auto"
_STANDBY_MODE: Final[str] = "charge_battery"
_CHARGE_MODE: Final[str] = "charge_battery"

# Entry gate — balance > 0.06 (compromise YAML trigger 0.07 / condition 0.04).
# Public — manager uses for balance range routing.
BALANCE_GATE_KWH: Final[float] = 0.06

# Exit < 0.05 (like YAML wait_template). Deadzone 0.05-0.06 — acceptable oscillation.
EXIT_BALANCE_KWH: Final[float] = 0.05

# SoC ceilings — entry lower than exit to avoid flapping when battery oscillates
# 99↔100 (small amount of energy to push in, intervention pointless). Exit
# stays at 100 — once intervention is active, we wait until actually fully
# charged.
SOC_ENTRY_CEILING: Final[int] = 99
SOC_CEILING: Final[int] = 100

# Pre-charge window: 7:00 → start_charge_hour_override (BatteryManager rules).
PRE_CHARGE_WINDOW_START_HOUR: Final[int] = 7

# PV low → STANDBY (night, no surplus).
PV_STANDBY_THRESHOLD_W: Final[int] = 200

# battery_charge_limit ≤ 7A → low BMS shortcut.
BMS_LOW_LIMIT_A: Final[int] = 7
# Low BMS shortcut — BMS clamp at ~2 kW, lookup unnecessary.
LOW_BMS_XSET_W: Final[int] = 3500

# Hysteresis for bucket transitions
HYSTERESIS_W: Final[int] = 300

# Adaptive charge lookup — `(lower, upper, xset)` (analog to NEGATIVE).
# Activates when `lower < pv_available <= upper` (top: upper=None=+inf).
# Bucket center yields meter ≈ -1500W (xset = center + 1500).
# pv_available ≤ -1000 → AUTO (block_discharge in battery.py takes over).
_ADAPTIVE_BUCKETS: Final[tuple[tuple[int, int | None, int], ...]] = (
    (4000, None, 6000),  # > 4000 W → charge 6000 (top, BMS practical max)
    (3000, 4000, 5000),  # charge 5000 (meter ~-1500 in center)
    (2000, 3000, 4000),
    (1000, 2000, 3000),
    (0, 1000, 2000),
    (-1000, 0, 1000),  # discharge approaching; pv_avail ≤ -1000 → AUTO
)


@dataclass
class PositiveIntervention:
    """Active POSITIVE intervention session.

    Mutable entity — `_continue` mutates recommended_*/last_reason via
    `_commit` helper. Manager does not create new instances per tick.
    """

    direction: ClassVar[InterventionDirection] = InterventionDirection.POSITIVE

    started_hour: int
    recommended_mode: str = _AUTO_MODE
    recommended_xset: int | None = None
    last_reason: str = ""
    _current_xset_for_hysteresis: int | None = field(default=None, repr=False)
    """Tracks last positive xset for hysteresis lookup. None when STANDBY/AUTO."""

    @classmethod
    def try_enter(
        cls,
        state: InputState,
        *,
        battery_charge_allowed: bool,
        start_charge_hour_override: time | None,
    ) -> EntryResult:
        """Try to enter POSITIVE intervention.

        Caller (GridExportManager) MUST verify global guards first:
        - balance > BALANCE_GATE_KWH (range routing)
        - no ems_interventions_blocked
        - not too_late_in_hour
        - not ems_schedule_active_this_hour

        Checks intervention-specific gates, then delegates to `_enter` for
        instance creation + initial _continue. `battery_charge_allowed` and
        `start_charge_hour_override` are sourced from BatteryChargeService
        (Etap B / B'-2 — replaces legacy `state.battery_charge_toggle_on` +
        `state.start_charge_hour_override`).
        """
        if cls._is_in_pre_charge_window(state, start_charge_hour_override):
            return EntryResult.blocked("in_pre_charge_window")
        if state.battery_soc >= SOC_ENTRY_CEILING:
            return EntryResult.blocked("soc_at_entry_ceiling")
        if not battery_charge_allowed:
            return EntryResult.blocked("charge_not_allowed")
        return cls._enter(state)

    @classmethod
    def _enter(cls, state: InputState) -> EntryResult:
        """Create blank intervention, run initial _continue, map to EntryResult.

        Initial _current_xset_for_hysteresis=None default → fresh lookup
        (no hysteresis on entry). _continue may signal exit if no real
        intervention is possible (e.g. pv_available is None) — propagated
        as EntryResult.blocked.
        """
        intervention = cls(started_hour=state.now.hour)
        result = intervention._continue(state)  # noqa: SLF001 — same-class access
        if result.is_exit:
            return EntryResult.blocked(result.exit_reason)
        return EntryResult.entered(intervention)

    def continue_or_exit(
        self,
        state: InputState,
        *,
        battery_charge_allowed: bool,
        start_charge_hour_override: time | None,
    ) -> ContinueResult:
        """Continue POSITIVE intervention. Mutates self in-place on continue.

        Caller (GridExportManager) MUST verify global guards first:
        - hour_rollover (started_hour mismatch)
        - end_of_hour_cleanup (now ≥ XX:59:50)
        - no ems_interventions_blocked
        """
        if self._is_in_pre_charge_window(state, start_charge_hour_override):
            return ContinueResult.exit_with("in_pre_charge_window")
        if state.exported_energy_hourly < EXIT_BALANCE_KWH:
            return ContinueResult.exit_with("balance_recovered")
        if state.battery_soc >= SOC_CEILING:
            return ContinueResult.exit_with("soc_ceiling_exit")
        if not battery_charge_allowed:
            return ContinueResult.exit_with("charge_not_allowed_exit")
        return self._continue(state)

    @staticmethod
    def _is_in_pre_charge_window(
        state: InputState, start_charge_hour_override: time | None
    ) -> bool:
        """Pre-charge: 7:00 ≤ now < start_charge_hour_override.

        Multi-caller helper (try_enter + continue_or_exit) — placed after
        the last caller (continue_or_exit). `start_charge_hour_override`
        passed by callers (Etap B'-2 — was read from InputState).
        """
        if start_charge_hour_override is None:
            return False
        if state.now.hour < PRE_CHARGE_WINDOW_START_HOUR:
            return False
        return state.now.time() < start_charge_hour_override

    def _continue(self, state: InputState) -> ContinueResult:
        """Compute new decision and commit to self. Returns CONTINUE or exit.

        Used by both try_enter (initial state, no hysteresis) and
        continue_or_exit (next state, hysteresis-aware via current xset).

        Decision priority:
        1. STANDBY — pv_for_standby < 200W
        2. Low BMS shortcut — battery_charge_limit ≤ 7A
        3. charge_adaptive — hysteresis → fresh lookup → AUTO fallback
        """
        # 1. PV low → STANDBY (instantaneous pv_power flaps, use 2-min avg;
        # fallback to instantaneous when avg=None — e.g. after HA restart).
        pv_for_standby = (
            state.pv_power_avg_2_minutes
            if state.pv_power_avg_2_minutes is not None
            else state.pv_power
        )
        if pv_for_standby < PV_STANDBY_THRESHOLD_W:
            return self._commit(_STANDBY_MODE, 0, "low_pv_standby", None)

        # 2. Low BMS shortcut — battery clamp ~2 kW, lookup unnecessary.
        # Does NOT require pv_available, so shortcut runs before that guard.
        if (
            state.battery_charge_limit is not None
            and state.battery_charge_limit <= BMS_LOW_LIMIT_A
        ):
            return self._commit(
                _CHARGE_MODE,
                LOW_BMS_XSET_W,
                f"charge_adaptive_low_bms_{LOW_BMS_XSET_W}W",
                None,
            )

        # 3. charge_adaptive lookup — requires pv_available.
        if state.pv_available is None:
            return ContinueResult.exit_with("none_pv_available")
        return self._resolve_charge_adaptive(state.pv_available)

    def _resolve_charge_adaptive(self, pv_available: float) -> ContinueResult:
        """Hysteresis stay → fresh lookup → AUTO fallback (pv_avail ≤ -1000).

        Initial state from try_enter: _current_xset_for_hysteresis=None →
        range=None → hysteresis skip → fresh lookup. Continue: hysteresis
        stabilizes oscillation at bucket boundaries.
        """
        # Hysteresis — current Xset stable when pv_available is in extended range.
        current_range = self._xset_range(self._current_xset_for_hysteresis)
        if current_range is not None:
            lower, upper = current_range
            if (lower - HYSTERESIS_W) < pv_available <= (upper + HYSTERESIS_W):
                current = self._current_xset_for_hysteresis
                return self._commit(
                    _CHARGE_MODE,
                    current,
                    f"charge_adaptive_stay_{current}W_pv_avail_{int(pv_available)}",
                    current,
                )

        # Fresh lookup
        xset = self._lookup_xset(pv_available)
        if xset is not None:
            return self._commit(
                _CHARGE_MODE,
                xset,
                f"charge_adaptive_{xset}W_pv_avail_{int(pv_available)}",
                xset,
            )

        # pv_available ≤ -1000 → mode=AUTO but stay in intervention (NOT exit).
        # Two-tier defense: POSITIVE first line (tries CHARGE_BATTERY at balance
        # > 60 Wh), battery.py second line (post-charge dual-trigger / afternoon-
        # dynamic) — block_discharge takes over when hourly balance is positive
        # despite POSITIVE failing to reduce it via CHARGE_BATTERY.
        return self._commit(
            _AUTO_MODE,
            None,
            f"charge_adaptive_auto_pv_avail_{int(pv_available)}",
            None,
        )

    @staticmethod
    def _xset_range(xset: int | None) -> tuple[float, float] | None:
        """Range of pv_available that would activate given Xset.

        Returns (lower, upper) or None when xset is not in _ADAPTIVE_BUCKETS
        (e.g. low_bms_shortcut 3500 — fallback to plain lookup).
        Top bucket has upper=inf.
        """
        if xset is None:
            return None
        for lower, upper, xs in _ADAPTIVE_BUCKETS:
            if xs == xset:
                upper_f = float("inf") if upper is None else float(upper)
                return (float(lower), upper_f)
        return None

    @staticmethod
    def _lookup_xset(pv_available: float) -> int | None:
        """Find Xset for pv_available in _ADAPTIVE_BUCKETS.

        Returns None when pv_available ≤ -1000 (outside the lowest bucket) —
        caller switches to AUTO mode (stay in intervention).
        """
        for lower, upper, xset in _ADAPTIVE_BUCKETS:
            if upper is None:
                if pv_available > lower:
                    return xset
            elif lower < pv_available <= upper:
                return xset
        return None

    def _commit(
        self,
        mode: str,
        xset: int | None,
        reason: str,
        current_xset_for_hysteresis: int | None,
    ) -> ContinueResult:
        """Mutate self with new decision and return CONTINUE.

        Multi-caller helper (_continue + _resolve_charge_adaptive) — placed
        after the last caller (_resolve_charge_adaptive) plus its sub-helpers.
        """
        self.recommended_mode = mode
        self.recommended_xset = xset
        self.last_reason = reason
        self._current_xset_for_hysteresis = current_xset_for_hysteresis
        return CONTINUE
