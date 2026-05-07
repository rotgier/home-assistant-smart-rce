"""Battery discharge management.

Pure domain — no HA imports, no logging. Persistence (Store) lives in
infrastructure adapter `BatteryStatePersistence` (driven outbound for
HA Storage). Logging lives in infrastructure adapter `BatteryManagerLogger`
(driven outbound for Python logging) — reads `diagnostic_snapshot()`.

Monitors hourly balance and pre/post-charge/afternoon windows to decide
when to block battery discharge. Block_charge handling moved to
GridExportManager (Stage 2 — NEGATIVE balance).

Phase classification + decisions (`update()` thin dispatcher → sub-method
per phase, each mutates `should_block_battery_discharge` + `_phase`):

- **override** (`ems_allow_discharge_override=True`): block=False unconditionally.
  EMS "stays out of the way" — lets other automations control battery freely.
- **pre-charge** (7:00 → `start_charge_hour_override`): workday only.
  Hysteresis 100/50 on `exported_energy_hourly` with `instant_surplus`
  extending keep-state zone. SET block=True when exported ≥ 100 Wh; forced
  reset when exported < 0 (hourly net import — NEGATIVE may take over);
  default reset when exported < 50 Wh AND no PV surplus; else keep state.
  instant_surplus (pv_5min > +500W) extends keep-state zone from dead zone
  (50-100 Wh) down to 0-100 Wh, avoiding DoD 0↔90 cycling at hour boundary
  when PV stays strong across transition (utility_meter resets exported
  each hour but PV surplus is continuous).
- **post-charge** (`start_charge_hour_override` → 13:00): workday only.
  Dual-trigger hysteresis: SET block=True when `instant_surplus` (PV trend
  +500W) OR `hourly_export ≥ 100 Wh`; RESET block=False when `instant_deficit`
  (PV trend <0W) AND `hourly_export < 50 Wh`; else keep state. Two-tier
  defense with POSITIVE intervention (POSITIVE first line at 60 Wh, battery.py
  second line at 100 Wh) — avoids battery cycling during positive hourly
  balance despite short-term deficits.
- **afternoon-static** (13:00 → 19:00, `rce_should_hold_for_peak=True`):
  block=False, automation Set Min SOC to 100 holds DoD=0 until 19:00.
- **afternoon-dynamic** (13:00 → 19:00, `rce_should_hold_for_peak=False`):
  Dual-trigger: SET block=True when `instant_surplus` OR `hourly_export > 0`;
  RESET block=False when `instant_deficit` AND NOT `hourly_export`; else keep.
  Aggressive thresholds (`> 0` Wh hourly) since we are past PV peak.
- **out-of-window** (<7:00, ≥19:00): block=False, evening discharge
  automations decide.

Each sub-method sets `self._phase` (str label for diagnostic) AND mutates
`should_block_battery_discharge` per branch logic. `diagnostic_snapshot(state)`
reads `_phase` field — does not recompute classification (single source of truth).

GridExport intervention activation thresholds (hourly net export, in Wh):

- POSITIVE (excessive export → CHARGE_BATTERY/STANDBY to absorb surplus)
  - Entry: hourly > +60 Wh (BALANCE_GATE_KWH = 0.06)
  - Exit:  hourly < +50 Wh (EXIT_BALANCE_KWH  = 0.05)
  - Hourly deadband: +50..+60 Wh (avoid flapping at boundary)
  - Plus SoC entry ceiling: blocked when battery_soc ≥ 99 (SOC_ENTRY_CEILING)
  - Plus SoC exit ceiling:  exits when battery_soc ≥ 100 (SOC_CEILING)

- NEGATIVE (net import → adaptive charge/discharge to stabilize +1500W export)
  - Entry pre-45min:  hourly < -50 Wh (ENTRY_THRESHOLD_EARLY_KWH = -0.05)
  - Entry post-45min: hourly <   0 Wh (ENTRY_THRESHOLD_LATE_KWH  =  0.00)
  - Exit:             hourly >   0 Wh (EXIT_BALANCE_KWH          =  0.00)
  - Plus SoC hard floor: entry blocked when battery_soc ≤ 10 (SOC_HARD_FLOOR)
  - DoD floor handling (asymmetric entry/continue with hysteresis):
    - Entry: discharge bucket + SoC ≤ (100 - DoD%) AND pv_available < 0
        → blocked (`soc_at_dod_floor_no_pv_surplus`). No surplus to redirect;
        AUTO/load-following more efficient than STOP.
    - Entry: discharge bucket + SoC at floor AND pv_available ≥ 0 → enters
        with bucket clamped to STOP (PV surplus redirects to grid as export).
    - Continue: discharge bucket + SoC at floor AND pv_available ≥ -200W
        → clamp to STOP (`DISCHARGE_FLOOR_HYSTERESIS_W`). Avoids flap when
        pv_available oscillates near zero.
    - Continue: discharge bucket + SoC at floor AND pv_available < -200W
        → exits (`soc_at_dod_floor_exit`). Deep deficit, STOP useless.

- Manager-level deadzone (-50..+60 Wh pre-45min, 0..+60 Wh post-45min):
  no intervention applies; last_decision_reason = "balance_in_deadzone_*".

Coordination matrix with GridExportManager (POSITIVE / NEGATIVE intervention):

    | Phase             | block_discharge trigger                | POSITIVE       | NEGATIVE |
    |-------------------|----------------------------------------|----------------|----------|
    | override          | False (always — EMS off)               | manager exits  | exits    |
    | pre-charge        | hourly ≥ 100 SET, < 0 forced reset,    | blocked        | yes      |
    |                   | < 50 + no surplus reset, else keep     | (in_pre_charge)|          |
    | post-charge       | instant_surplus OR hourly ≥ 100 SET;   | yes            | yes      |
    |                   | instant_deficit AND hourly < 50 RESET; |                |          |
    |                   | else keep                              |                |          |
    | afternoon-static  | False (DoD=0 until 19:00 by automation)| typically      | yes      |
    |                   |                                        | blocked by SoC |          |
    |                   |                                        | ceiling        |          |
    | afternoon-dynamic | instant_surplus OR hourly > 0 SET;     | yes            | yes      |
    |                   | instant_deficit AND hourly <= 0 RESET; |                |          |
    |                   | else keep                              |                |          |
    | out-of-window     | False (always)                         | yes (rare)     | yes      |

Two-tier defense (post-charge, afternoon-dynamic):
- POSITIVE first line — `try_enter` at balance > 60 Wh, tries to absorb the
  surplus via CHARGE_BATTERY xset.
- battery.py second line — block=True at `hourly_export ≥ 100 Wh` prevents
  battery discharge during sustained positive balance, even when PV deficit
  is transient. Kicks in when POSITIVE cannot reduce balance (SoC=100% / BMS
  clamp / sustained surplus).
- Threshold hierarchy: POSITIVE entry 60 Wh, battery SET 100 Wh — clear
  precedence (POSITIVE acts first, battery.py reinforces).

Out-of-window / afternoon-static — BatteryManager "stays out of the way":
- afternoon-static: Set Min SOC=100 automation holds DoD=0; battery typically
  full → POSITIVE entry blocked by `soc_at_entry_ceiling` (rare to be active).
  When active, POSITIVE may have any mode (CHARGE/STANDBY/AUTO) — physical
  discharge prevented by DoD=0 anyway.
- out-of-window (≥19:00): evening discharge automations rule (peak discharge,
  Set SOC 90 at 19:00). Battery.block=False (always). POSITIVE rare here
  (hourly export > 0.06 unusual at night/evening) — when active, any mode.
- mode=AUTO is a specific POSITIVE fallback (`pv_available ≤ -1000W` in
  `_resolve_charge_adaptive`) — can occur in any phase, not phase-specific.

Pre-charge (POSITIVE blocked, NEGATIVE allowed):
- POSITIVE explicitly blocked by `in_pre_charge_window` gate (BatteryManager
  rules via block_discharge hysteresis on hourly export).
- NEGATIVE may activate if balance < entry_threshold (independent concern —
  battery can discharge into deficit if SoC > min_soc).

Downstream — DoD propagation (how block_discharge becomes inverter setting):

`should_block_battery_discharge` is exposed as
`binary_sensor.ems_block_battery_discharge` (via SensorEntityDescription in
custom_components/smart_rce/binary_sensor.py). HA-side automation
`ems-set-dod-from-block-discharge` translates state changes into
`number.goodwe_depth_of_discharge_on_grid`:
- block=True  → DoD=0  (min_SoC=100%, battery cannot discharge)
- block=False → DoD=90 (min_SoC=10%,  battery may discharge to 10%)

In phases where BatteryManager intentionally "stays out of the way", dedicated
HA automations rule the DoD setting instead:

    | Phase             | block_discharge | DoD setter                                  |
    |-------------------|-----------------|---------------------------------------------|
    | override          | False (always)  | other automations (BatteryManager off)      |
    | pre-charge        | hysteresis      | ems-set-dod-from-block-discharge            |
    | post-charge       | dual-trigger    | ems-set-dod-from-block-discharge            |
    | afternoon-static  | False (off)     | "Set DoD 0 at 13:00 if hold_for_peak"       |
    | afternoon-dynamic | dual-trigger    | ems-set-dod-from-block-discharge            |
    | out-of-window     | False (always)  | evening automations (Set SOC 90 at 19:00)   |

One-way flow (read-only consumer): smart_rce declares intent via the binary
sensor; HA automations map intent to inverter registers. No feedback loop.

See `context/target_soc_algorithm.md` for broader context.
"""

from __future__ import annotations

from typing import Any, Final

from custom_components.smart_rce.domain.input_state import InputState

# --- discharge guard (pre-charge window) --- #

# Pre-charge window start (hour).
PRE_CHARGE_WINDOW_START_HOUR: Final[int] = 7

# Hysteresis MINE for block_discharge: SET at >=100 Wh, RESET at <50 Wh.
# Dead zone 50..100 keeps previous state.
DISCHARGE_HYSTERESIS_SET_WH: Final[int] = 100
DISCHARGE_HYSTERESIS_RESET_WH: Final[int] = 50

# --- discharge guard (post-charge window) --- #

# Post-charge window end (hour). Window: start_charge_hour_override → 13:00.
POST_CHARGE_WINDOW_END_HOUR: Final[int] = 13

# --- discharge guard (afternoon window) --- #

# Afternoon window: 13:00 → 19:00. After 19:00 existing automations take over
# (Set SOC 90 at 19, evening discharge). Dynamic block_discharge active only
# when state.rce_should_hold_for_peak=False (low-price upcoming peaks).
AFTERNOON_WINDOW_START_HOUR: Final[int] = 13
AFTERNOON_WINDOW_END_HOUR: Final[int] = 19

# Continuous check on `pv_available_5min` (W; = -consumption_minus_pv_5_minutes):
# positive = PV > cons (surplus), negative = cons > PV (deficit).
# Hysteresis triggers block_discharge on sustained surplus >500W,
# resets on sustained deficit <0W. Dead zone 0..500 → keep state.
PV_AVAIL_5MIN_SURPLUS_W: Final[int] = 500
PV_AVAIL_5MIN_DEFICIT_W: Final[int] = 0


class BatteryManager:
    def __init__(self) -> None:
        self.should_block_battery_discharge: bool = False
        # _phase set by update() (thin dispatcher → sub-method).
        # Initial "none-present" — until first update with full state.
        self._phase: str = "none-present"

    def update(self, state: InputState) -> None:
        """Thin dispatcher — classifies phase, delegates to sub-method.

        Each sub-method is responsible for:
        1. Setting `self._phase` (diagnostic label)
        2. Mutating `should_block_battery_discharge` per window logic
        """
        if self._none_present(state):
            self._phase = "none-present"
            return

        if state.ems_allow_discharge_override is True:
            self._update_override()
        elif self._is_in_pre_charge_window(state):
            self._update_pre_charge(state)
        elif self._is_in_post_charge_window(state):
            self._update_post_charge(state)
        elif self._is_in_afternoon_window(state):
            self._update_afternoon(state)
        else:
            self._update_out_of_window()

    def _update_override(self) -> None:
        """OVERRIDE: intentional discharge (e.g. Battery Discharge Max).

        When input_boolean.ems_allow_discharge_override=True, EMS "stays out
        of the way". block_discharge forced to False — lets other automations
        control battery freely without interference.
        """
        self._phase = "override"
        self.should_block_battery_discharge = False

    def _update_pre_charge(self, state: InputState) -> None:
        """Pre-charge (7:00 → start_charge_hour): hysteresis with instant_surplus extension.

        Decision tree (no separate hour-boundary branch — hysteresis handles
        all cases including hour boundary):
        - exported ≥ 100 Wh → SET block=True (sustained export, hold battery)
        - exported < 0 → forced reset (hourly net import, NEGATIVE may handle)
        - exported < 50 AND no PV surplus → reset (default below-threshold)
        - else (50..100 dead zone OR 0..50 with PV surplus) → keep state

        instant_surplus (pv_5min > +500W) extends keep-state zone from dead
        zone (50-100 Wh) to 0-100 Wh — avoids DoD 0↔90 cycling at hour
        boundary when PV stays strong across transition (utility_meter
        resets exported each hour but PV surplus is continuous).
        """
        if state.is_workday is None:
            # Defensive: workday sensor not loaded yet (typically 25-50ms
            # after HA restart). Keep state — wait for sensor to settle.
            # Without this a spurious reset could happen.
            self._phase = "pre-charge-keep-state"
            return
        if state.is_workday is False:
            # Weekend/holiday — passthrough (RCE flat, no expensive hours)
            self._phase = "pre-charge-passthrough"
            self.should_block_battery_discharge = False
            return

        self._phase = "pre-charge"
        exported_wh = state.exported_energy_hourly * 1000  # kWh → Wh
        pv_5min = state.pv_available_5min
        instant_surplus = pv_5min is not None and pv_5min > PV_AVAIL_5MIN_SURPLUS_W

        if exported_wh >= DISCHARGE_HYSTERESIS_SET_WH:
            self.should_block_battery_discharge = True
        elif exported_wh < 0:
            # Forced reset — hourly net import (NEGATIVE may take over)
            self.should_block_battery_discharge = False
        elif exported_wh < DISCHARGE_HYSTERESIS_RESET_WH and not instant_surplus:
            # Default below-threshold reset (no PV surplus to extend keep zone)
            self.should_block_battery_discharge = False
        # else: keep state (dead zone 50..100 OR 0..50 with instant_surplus)

    def _update_post_charge(self, state: InputState) -> None:
        """Post-charge (start_charge_hour → 13:00): dual-trigger (instant + hourly).

        Two-tier defense with POSITIVE intervention (`grid_export/positive.py`):
        - POSITIVE first line — try_enter at `balance > 60 Wh`, tries to reduce
          balance via CHARGE_BATTERY xset.
        - Battery.py second line — when POSITIVE cannot handle (e.g. SoC=100% /
          BMS clamp / sustained surplus), block=True at `hourly_export ≥ 100 Wh`
          prevents battery discharge during positive hourly balance.

        Triggers (analog of pre-charge thresholds + afternoon-dynamic dual-trigger):
        - SET (block=True): `instant_surplus` (pv_5min > +500W) OR
          `hourly_export ≥ 100 Wh`
        - RESET (block=False): `instant_deficit` (pv_5min < 0W) AND
          `hourly_export < 50 Wh`
        - Else keep state (dead zone instant 0-500 / hourly 50-100)
        """
        if state.is_workday is None:
            # Defensive — see pre-charge.
            self._phase = "post-charge-keep-state"
            return
        if state.is_workday is False:
            # Weekend/holiday — passthrough
            self._phase = "post-charge-passthrough"
            self.should_block_battery_discharge = False
            return

        self._phase = "post-charge"
        pv_available_5min = state.pv_available_5min
        exported_wh = state.exported_energy_hourly * 1000  # kWh → Wh
        if pv_available_5min is None:
            self.should_block_battery_discharge = False
            return
        instant_surplus = pv_available_5min > PV_AVAIL_5MIN_SURPLUS_W
        instant_deficit = pv_available_5min < PV_AVAIL_5MIN_DEFICIT_W
        hourly_set = exported_wh >= DISCHARGE_HYSTERESIS_SET_WH
        hourly_reset = exported_wh < DISCHARGE_HYSTERESIS_RESET_WH
        if instant_surplus or hourly_set:
            self.should_block_battery_discharge = True
        elif instant_deficit and hourly_reset:
            self.should_block_battery_discharge = False
        # else: keep state (dead zone in either dimension)

    def _update_afternoon(self, state: InputState) -> None:
        """Afternoon (13:00 → 19:00): static (high-price) or dynamic (low-price)."""
        if state.rce_should_hold_for_peak is None:
            # Defensive: hold sensor not loaded yet. Without this guard the
            # 14:33 bug returns — first update sees None, falls into dynamic
            # branch, sets block_discharge=True; ~22ms later sensor loads
            # as on, BatteryManager switches to static and sets False;
            # automation reacts to on→off and sets DoD=90.
            self._phase = "afternoon-keep-state"
            return

        if state.rce_should_hold_for_peak is True:
            # High-price mode — status quo, automation Set Min SOC to 100
            # Afternoon holds DoD=0 until 19:00. BatteryManager does not steer.
            self._phase = "afternoon-static"
            self.should_block_battery_discharge = False
            return

        # Low-price mode — dynamic on pv_available_5min OR exported_wh.
        # SET (hold): instant_surplus OR hourly_net_export
        # RESET (allow): instant_deficit AND NOT hourly_net_export
        # Other combinations (dead zone) → keep state
        self._phase = "afternoon-dynamic"
        pv_available_5min = state.pv_available_5min
        exported_wh = state.exported_energy_hourly * 1000
        if pv_available_5min is None:
            self.should_block_battery_discharge = False
            return
        instant_surplus = pv_available_5min > PV_AVAIL_5MIN_SURPLUS_W
        instant_deficit = pv_available_5min < PV_AVAIL_5MIN_DEFICIT_W
        hourly_net_export = exported_wh > 0
        if instant_surplus or hourly_net_export:
            self.should_block_battery_discharge = True
        elif instant_deficit and not hourly_net_export:
            self.should_block_battery_discharge = False
        # else: keep state

    def _update_out_of_window(self) -> None:
        """Outside all windows (before 7:00 or after 19:00): reset."""
        self._phase = "out-of-window"
        self.should_block_battery_discharge = False

    @staticmethod
    def _is_in_pre_charge_window(state: InputState) -> bool:
        """Pre-charge: 7:00 ≤ now < start_charge_hour_override (minute precision)."""
        if state.start_charge_hour_override is None or state.now is None:
            return False
        if state.now.hour < PRE_CHARGE_WINDOW_START_HOUR:
            return False
        # Compare time (hh:mm:ss) to handle override like 10:30
        return state.now.time() < state.start_charge_hour_override

    @staticmethod
    def _is_in_post_charge_window(state: InputState) -> bool:
        """Post-charge: start_charge_hour_override ≤ now < 13:00."""
        if state.start_charge_hour_override is None or state.now is None:
            return False
        if state.now.hour >= POST_CHARGE_WINDOW_END_HOUR:
            return False
        return state.now.time() >= state.start_charge_hour_override

    @staticmethod
    def _is_in_afternoon_window(state: InputState) -> bool:
        """Afternoon: 13:00 ≤ now < 19:00."""
        if state.now is None:
            return False
        return AFTERNOON_WINDOW_START_HOUR <= state.now.hour < AFTERNOON_WINDOW_END_HOUR

    @staticmethod
    def _none_present(state: InputState) -> bool:
        return state.exported_energy_hourly is None or state.now is None

    # --- public state APIs ---

    def snapshot(self) -> dict[str, Any]:
        """Pure state snapshot — used by infrastructure adapter for persistence."""
        return {
            "block_discharge": self.should_block_battery_discharge,
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Pure restore from dict — used by infrastructure adapter at startup.

        Ignores legacy "last_hour_seen" key from older persisted snapshots
        (field removed; safe to drop silently).
        """
        self.should_block_battery_discharge = data.get("block_discharge", False)

    def diagnostic_snapshot(self, state: InputState) -> dict[str, Any]:
        """Log-relevant view: phase (FIELD set by update) + decision + key inputs.

        Reads `self._phase` field (set by last `update()`) — does not
        recompute classification. `state` provides current input values
        for DEBUG snapshot log.

        Used by `BatteryManagerLogger` (infrastructure/battery_logger.py)
        — registered as `ems.async_add_listener`.
        """
        return {
            "phase": self._phase,
            "block_discharge": self.should_block_battery_discharge,
            # InputState fields — selective (those printed in DEBUG)
            "now": state.now,
            "exported_energy_hourly": state.exported_energy_hourly,
            "pv_available_5min": state.pv_available_5min,
            "depth_of_discharge": state.depth_of_discharge,
            "battery_charge_toggle_on": state.battery_charge_toggle_on,
            "battery_charge_limit": state.battery_charge_limit,
            "start_charge_hour_override": state.start_charge_hour_override,
            "ems_allow_discharge_override": state.ems_allow_discharge_override,
        }
