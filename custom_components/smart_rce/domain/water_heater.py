"""Water Heater Manager — DHW (CWU) heater on/off decisions.

Controls DHW heaters (BIG 3kW, SMALL 1.5kW) — BALANCED-only logic:
- PV surplus (sensor minus_pv)
- Battery SOC + battery_charge_limit (how much battery accepts)
- Active GridExportManager intervention (POSITIVE/NEGATIVE — bigger reserved)
- User override `prefer_battery_first` (heaters only run when bonus is meaningful)

Two-tier decision:
1. Baseline — PV self-coverage after subtracting `reserved` per
   battery_charge_limit + intervention
2. Upgrade — adaptive lift to consume the hourly export bonus (so we don't
   waste already-exported Wh)

NEGATIVE intervention forces higher reserved (heaters off priority) because a
3kW heater is typically the main driver of hourly deficit.

`prefer_battery_first` override: when True, user prefers maxing battery charge.
Heaters fire ONLY when export_bonus passes the threshold (≥1000W; held by
hysteresis ≥500W) — i.e. only when recovering already-exported Wh, not firing
heaters "from baseline PV alone". Plus reserved escalates to max battery
capability per tier (2000W at >2, 600W at ==2). Exception: POSITIVE intervention
does NOT escalate (battery already gets surplus via the intervention, heaters
can run too).

File layout (Java-style): WaterHeaterManager public class at TOP, then private
HeaterState enum + module-level constants BELOW.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from functools import total_ordering

from custom_components.smart_rce.domain.grid_export import InterventionDirection
from custom_components.smart_rce.domain.input_state import InputState

# Adaptive upgrade consuming export budget over the rest of the hour.
# Bonus = exported_energy_so_far / seconds_until_hour_end — converts the
# already-exported Wh into equivalent extra power available for consumption.
EXPORT_BONUS_CUTOFF_SEC: int = 60  # below this don't activate bonus (last minute)
EXPORT_BONUS_MIN_T_LEFT_SEC: int = 60  # lower clamp for division
LADDER_HYSTERESIS_W: int = 500  # used for both baseline and upgrade ladder

# Mode-specific bonus gate (only active when prefer_battery_first=True).
# Gate opens at ≥1000W (real export to recover); held open down to 500W via
# hysteresis. Below threshold in battery-first mode → heaters OFF.
BONUS_GATE_ON_W: int = 1000
BONUS_GATE_OFF_W: int = 500


def seconds_until_hour_end(now: datetime) -> int:
    return 3600 - (now.minute * 60 + now.second)


class WaterHeaterManager:
    """Public API. Orchestrates baseline + upgrade decision per tick."""

    def __init__(self) -> None:
        self.should_turn_on: bool = False
        self.should_turn_off: bool = False
        self.should_turn_on_small: bool = False
        self.should_turn_off_small: bool = False
        # Diagnostics — exposed as HA sensors. Stored as canonical strings
        # (state.canonical) for direct HA state-machine compatibility.
        self.heater_budget: float | None = None
        self.heater_baseline: str | None = None
        self.heater_upgrade_target: str | None = None
        self.heater_upgrade_active: bool = False
        self.heater_export_bonus: float | None = None
        # True when prefer_battery_first=True AND bonus gate is open. Signals
        # "heater allowed to run despite battery-first preference because
        # real export bonus needs recovering".
        self.heater_running_via_bonus: bool = False

    def update(
        self,
        state: InputState,
        grid_export_intervention: InterventionDirection | None = None,
        *,
        battery_charge_allowed: bool,
        reserved_balanced_full: int = 5500,
        prefer_battery_first: bool = False,
    ) -> None:
        """Compute target heater state + set should_turn_on/_off flags.

        `grid_export_intervention` (POSITIVE/NEGATIVE/None):
        - POSITIVE: battery catches surplus, reserved raised to 3500W
          (`charge_limit > 7`) to protect battery intervention.
        - NEGATIVE: hourly deficit — higher reserved to force heaters off.
        - None: original logic.

        `battery_charge_allowed`: kwarg from BatteryChargeService — replaces
        legacy `state.battery_charge_toggle_on`. When False, effective
        charge_limit is treated as 0 (battery idle, PV fully available
        for heaters).

        `prefer_battery_first`: user-controlled override. When True:
        - Reserved escalates to max battery capability per tier (>7: 5500;
          >2: 2000; ==2: 600) — except under POSITIVE intervention.
        - At `battery_charge_limit > 2`: bonus gate applies. Heaters fire
          ONLY when export_bonus ≥1000W (or ≥500W via hysteresis when
          currently on). Otherwise target=OFF.
        - At `battery_charge_limit <= 2`: gate IGNORED (legacy semantic —
          battery near-full, no heater-vs-battery conflict).
        """
        if self._none_present(state):
            return

        current_state = self._current_state(state)
        target = self.target(
            state,
            current_state,
            grid_export_intervention=grid_export_intervention,
            battery_charge_allowed=battery_charge_allowed,
            reserved_balanced_full=reserved_balanced_full,
            prefer_battery_first=prefer_battery_first,
        )

        self.should_turn_on = target.big_on
        self.should_turn_off = not target.big_on
        self.should_turn_on_small = target.small_on
        self.should_turn_off_small = not target.small_on

    def target(
        self,
        state: InputState,
        current_state: HeaterState,
        *,
        grid_export_intervention: InterventionDirection | None = None,
        battery_charge_allowed: bool = True,
        reserved_balanced_full: int = 5500,
        prefer_battery_first: bool = False,
    ) -> HeaterState:
        """Pure decision — BALANCED two-tier logic with optional battery-first override.

        Thin orchestrator delegating to extracted helpers (per Rule 1 —
        callees ordered by call sequence below). Returns the target
        `HeaterState`; side-effects diagnostic fields via `_set_diagnostics`.
        """
        # When charge disabled (pre-charge window), treat as 0 regardless of
        # BMS hardware cap. BatteryChargeService.charge_allowed is single source.
        battery_charge_limit = (
            0.0 if not battery_charge_allowed else state.battery_charge_limit
        )
        exported_energy_wh = state.exported_energy_hourly * 1000

        reserved = self._compute_reserved(
            battery_charge_limit=battery_charge_limit,
            grid_export_intervention=grid_export_intervention,
            prefer_battery_first=prefer_battery_first,
            reserved_balanced_full=reserved_balanced_full,
        )
        heater_budget = state.pv_available - reserved
        baseline = self._ladder(heater_budget, current_state, LADDER_HYSTERESIS_W)

        skip_upgrade = battery_charge_limit > 7 and not prefer_battery_first
        export_bonus = self._compute_export_bonus(
            exported_energy_wh=exported_energy_wh,
            now=state.now,
            skip_upgrade=skip_upgrade,
        )
        effective_budget = heater_budget + export_bonus
        upgrade_candidate = self._ladder(
            effective_budget, current_state, LADDER_HYSTERESIS_W
        )

        battery_first_active = prefer_battery_first and battery_charge_limit > 2
        bonus_gate_open = self._bonus_gate_open(export_bonus, current_state)
        target = self._resolve_target(
            baseline=baseline,
            upgrade_candidate=upgrade_candidate,
            battery_first_active=battery_first_active,
            bonus_gate_open=bonus_gate_open,
        )

        self._set_diagnostics(
            heater_budget=heater_budget,
            baseline=baseline,
            target=target,
            export_bonus=export_bonus,
            heater_running_via_bonus=battery_first_active and bonus_gate_open,
        )
        return target

    # ─── Pre-condition + state read (called by `update`) ───────────────────

    @staticmethod
    def _none_present(state: InputState) -> bool:
        return (
            state.water_heater_big_is_on is None
            or state.water_heater_small_is_on is None
            or state.battery_soc is None
            or state.battery_charge_limit is None
            or state.battery_power_2_minutes is None
            or state.consumption_minus_pv_2_minutes is None
            or state.exported_energy_hourly is None
            or state.now is None
        )

    @staticmethod
    def _current_state(state: InputState) -> HeaterState:
        if state.water_heater_big_is_on and state.water_heater_small_is_on:
            return HeaterState.BOTH
        if state.water_heater_big_is_on:
            return HeaterState.BIG
        if state.water_heater_small_is_on:
            return HeaterState.SMALL
        return HeaterState.OFF

    # ─── target() helpers (Rule 1a — in call order) ───────────────────────

    @staticmethod
    def _compute_reserved(
        *,
        battery_charge_limit: float,
        grid_export_intervention: InterventionDirection | None,
        prefer_battery_first: bool,
        reserved_balanced_full: int,
    ) -> int:
        """Reserved power (W) per battery_charge_limit tier + intervention.

        `high_reserve` = negative intervention always escalates (force heaters
        off); prefer_battery_first also escalates EXCEPT under positive
        intervention (battery already gets surplus via the intervention).
        """
        # `==` instead of `is` — StrEnum value-based compare survives
        # live_reload() of grid_export module (water_heater may hold an OLD
        # InterventionDirection class reference; `is` fails despite same value).
        is_positive = grid_export_intervention == InterventionDirection.POSITIVE
        is_negative = grid_export_intervention == InterventionDirection.NEGATIVE
        high_reserve = (is_negative or prefer_battery_first) and not is_positive

        if battery_charge_limit > 7:
            if is_positive:
                return 3500
            if high_reserve:
                return 5500  # battery max draw; heaters MUST be off
            return reserved_balanced_full
        if battery_charge_limit > 2:
            return 2000 if high_reserve else 1000
        if battery_charge_limit == 2:
            return 600 if high_reserve else 300
        if battery_charge_limit == 1:
            return 300
        return 0  # battery_charge_limit == 0

    @staticmethod
    def _ladder(
        budget: float, current_state: HeaterState, hysteresis: int
    ) -> HeaterState:
        """Pick highest HeaterState whose power threshold fits in `budget`.

        Hysteresis: `current_state` is held if budget is within `hysteresis`
        below its power threshold (prevents flap on noise around boundary).
        Shared by baseline (budget = heater_budget) and upgrade
        (budget = heater_budget + export_bonus).
        """
        for state in (HeaterState.BOTH, HeaterState.BIG, HeaterState.SMALL):
            if budget >= state.power or (
                budget >= state.power - hysteresis and current_state == state
            ):
                return state
        return HeaterState.OFF

    @staticmethod
    def _compute_export_bonus(
        *,
        exported_energy_wh: float,
        now: datetime,
        skip_upgrade: bool,
    ) -> float:
        """Equivalent W needed to consume exported_energy_wh in the remaining hour.

        Returns 0 when `skip_upgrade` (battery wants max charge, no override)
        or in the last EXPORT_BONUS_CUTOFF_SEC seconds (unrealistic to consume
        meaningful kWh in the final minute; avoid last-second jolt before
        utility_meter resets).

        No cap on BOTH_POWER — bonus may exceed max heater draw; the ladder
        won't pick a state above BOTH_ARE_ON anyway. A cap would block
        BOTH_ARE_ON activation when heater_budget is negative and a large
        export accumulated near the end of the hour.
        """
        if skip_upgrade:
            return 0.0
        seconds_left = seconds_until_hour_end(now)
        if seconds_left < EXPORT_BONUS_CUTOFF_SEC:
            return 0.0
        t_left_h = max(seconds_left, EXPORT_BONUS_MIN_T_LEFT_SEC) / 3600
        return max(0.0, exported_energy_wh / t_left_h)

    @staticmethod
    def _bonus_gate_open(export_bonus: float, current_state: HeaterState) -> bool:
        """Mode-specific gate: True when bonus ≥1000W (or ≥500W via hysteresis).

        Used only when `prefer_battery_first=True` to filter out small
        noise-bonus from briefly turning heaters on.
        """
        return export_bonus >= BONUS_GATE_ON_W or (
            current_state != HeaterState.OFF and export_bonus >= BONUS_GATE_OFF_W
        )

    @staticmethod
    def _resolve_target(
        *,
        baseline: HeaterState,
        upgrade_candidate: HeaterState,
        battery_first_active: bool,
        bonus_gate_open: bool,
    ) -> HeaterState:
        if battery_first_active and not bonus_gate_open:
            return HeaterState.OFF
        if upgrade_candidate > baseline:
            return upgrade_candidate
        return baseline

    def _set_diagnostics(
        self,
        *,
        heater_budget: float,
        baseline: HeaterState,
        target: HeaterState,
        export_bonus: float,
        heater_running_via_bonus: bool,
    ) -> None:
        """Write diagnostic fields read by HA sensors.

        Stores `.label` strings (not HeaterState objects) for direct HA
        state-machine compatibility — sensor `native_value` returns the
        string as-is, no implicit stringification needed.
        """
        # Sign-flipped for diagnostic display: positive value means "deficit"
        # (pv_available below reserved); negative means "surplus available
        # for heaters" (the higher tier of the ladder).
        self.heater_budget = -heater_budget
        self.heater_baseline = baseline.label
        self.heater_upgrade_active = target != baseline
        if target == baseline:
            self.heater_upgrade_target = f"{baseline.label} (baseline)"
        else:
            self.heater_upgrade_target = f"{baseline.label} -> {target.label}"
        self.heater_export_bonus = export_bonus
        self.heater_running_via_bonus = heater_running_via_bonus


# ─── Private value objects (file-local) ────────────────────────────────────


@total_ordering
class HeaterState(Enum):
    """One of 4 heater states — Java-like enum with per-member attributes.

    Pure Enum (NOT IntEnum) — type-safe, member is NOT an int. Per-member
    attributes (power, label, big_on, small_on) set via `__init__` from the
    value tuple. Comparison via @total_ordering + __lt__ on `power`.

    Pattern: Planet example in https://docs.python.org/3/howto/enum.html
    """

    # power_w, label,   big_on, small_on
    OFF = (0, "off", False, False)
    SMALL = (1500, "small", False, True)
    BIG = (3000, "big", True, False)
    BOTH = (4500, "both", True, True)

    def __init__(
        self,
        power: int,
        label: str,
        big_on: bool,
        small_on: bool,
    ) -> None:
        self.power = power
        self.label = label
        self.big_on = big_on
        self.small_on = small_on

    def __str__(self) -> str:
        """HA sensor compat — str(state) returns the short label."""
        return self.label

    def __lt__(self, other) -> bool:
        if self.__class__ is other.__class__:
            return self.power < other.power
        return NotImplemented
