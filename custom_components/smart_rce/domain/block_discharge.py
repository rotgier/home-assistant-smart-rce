"""Block-discharge hysteresis algorithms — pure stateless functions.

Each function takes (state, prev_block) → bool. No phase awareness, no time
tracking, no class state. Phase ownership lives in DodPolicy; these functions
implement the *content* of "should we block discharge" given current sensor
inputs + previous decision (for hysteresis keep-state).

Three phases require dynamic decisions:

- pre-charge (7:00 → start_charge_hour, workday)
- post-charge (start_charge_hour → 13:00, workday)
- afternoon-dynamic (13:00 → 19:00, low-price peak)

The remaining phases use direct rules in DodPolicy (DoD = 0 or 90 by phase
identity, no hysteresis).

Defensive handling: when `exported_energy_hourly` is None (sensor missing),
all functions return `prev_block` to keep state until the input recovers.
`pv_available_5min` is None-safe — treated as "no instant signal" so only
hourly hysteresis decides.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .input_state import InputState


# --- Hysteresis thresholds (Wh) --- #

# SET at >=100 Wh, RESET at <50 Wh. Dead zone 50..100 keeps previous state.
DISCHARGE_HYSTERESIS_SET_WH: Final[int] = 100
DISCHARGE_HYSTERESIS_RESET_WH: Final[int] = 50

# --- PV instant signals (W) --- #

# Continuous check on `pv_available_5min` (W; = -consumption_minus_pv_5_minutes):
# positive = PV > cons (surplus), negative = cons > PV (deficit).
PV_AVAIL_5MIN_SURPLUS_W: Final[int] = 500
PV_AVAIL_5MIN_DEFICIT_W: Final[int] = 0


def block_pre_charge(state: InputState, prev_block: bool) -> bool:
    """Pre-charge hysteresis with instant_surplus extension.

    SET (True):           exported_wh >= 100
    Forced reset (False): exported_wh < 0  (NEGATIVE may take over)
    Default reset (False): exported_wh < 50 AND not instant_surplus
    Else: keep prev_block

    instant_surplus (pv_5min > +500W) extends keep-state zone from dead zone
    (50..100 Wh) to (0..100 Wh) — avoids DoD 0↔90 cycling at hour boundary
    when PV stays strong across utility_meter reset (exported_wh resets each
    hour but PV surplus is continuous).

    Defensive: exported_energy_hourly=None → return prev_block (sensor missing).
    """
    if state.exported_energy_hourly is None:
        return prev_block

    exported_wh = state.exported_energy_hourly * 1000  # kWh → Wh
    pv_5min = state.pv_available_5min
    instant_surplus = pv_5min is not None and pv_5min > PV_AVAIL_5MIN_SURPLUS_W

    if exported_wh >= DISCHARGE_HYSTERESIS_SET_WH:
        return True
    if exported_wh < 0:
        # Forced reset — hourly net import (NEGATIVE may take over)
        return False
    if exported_wh < DISCHARGE_HYSTERESIS_RESET_WH and not instant_surplus:
        # Default below-threshold reset (no PV surplus to extend keep zone)
        return False
    return prev_block  # keep state (dead zone, or 0..50 with instant_surplus)


def block_post_charge(state: InputState, prev_block: bool) -> bool:
    """Post-charge dual-trigger (instant + hourly).

    SET (True):    instant_surplus OR hourly_export >= 100
    RESET (False): instant_deficit AND hourly_export < 50
    Else: keep prev_block

    Two-tier defense with POSITIVE intervention: POSITIVE first line at
    hourly > 60 Wh tries to absorb surplus via CHARGE_BATTERY xset; this fn
    second line at hourly >= 100 Wh prevents discharge when POSITIVE cannot
    handle (e.g. SoC=100%, BMS clamp, sustained surplus).

    Defensive: pv_available_5min=None → treat as no instant signal (mirrors
    pre-charge — transient sensor unavailability would otherwise spuriously
    flip block to False, causing 0↔90 DoD flicker).
    Defensive: exported_energy_hourly=None → return prev_block.
    """
    if state.exported_energy_hourly is None:
        return prev_block

    pv_5min = state.pv_available_5min
    exported_wh = state.exported_energy_hourly * 1000
    instant_surplus = pv_5min is not None and pv_5min > PV_AVAIL_5MIN_SURPLUS_W
    instant_deficit = pv_5min is not None and pv_5min < PV_AVAIL_5MIN_DEFICIT_W
    hourly_set = exported_wh >= DISCHARGE_HYSTERESIS_SET_WH
    hourly_reset = exported_wh < DISCHARGE_HYSTERESIS_RESET_WH

    if instant_surplus or hourly_set:
        return True
    if instant_deficit and hourly_reset:
        return False
    return prev_block  # dead zone in either dimension, or pv=None


def block_afternoon_dynamic(state: InputState, prev_block: bool) -> bool:
    """Afternoon-dynamic dual-trigger (aggressive — past PV peak).

    SET (True):    instant_surplus OR hourly_export > 0
    RESET (False): instant_deficit AND hourly_export <= 0
    Else: keep prev_block

    Lower hourly thresholds than post-charge (0 vs 50/100 Wh) since PV peak
    has passed — even small surplus is meaningful, deficit needs a decisive
    trigger.

    Defensive: pv_available_5min=None → no instant signal (only hourly decides).
    Defensive: exported_energy_hourly=None → return prev_block.
    """
    if state.exported_energy_hourly is None:
        return prev_block

    pv_5min = state.pv_available_5min
    exported_wh = state.exported_energy_hourly * 1000
    instant_surplus = pv_5min is not None and pv_5min > PV_AVAIL_5MIN_SURPLUS_W
    instant_deficit = pv_5min is not None and pv_5min < PV_AVAIL_5MIN_DEFICIT_W
    hourly_net_export = exported_wh > 0

    if instant_surplus or hourly_net_export:
        return True
    if instant_deficit and not hourly_net_export:
        return False
    return prev_block  # dead zone, or pv=None with no hourly export
