"""Battery charge/discharge management.

Monitoruje bilans godzinowy (`exported_energy_hourly`) i decyduje kiedy
zablokować ładowanie/rozładowywanie baterii.

Decyzje:

- `should_block_battery_charge` — aktywny gdy bilans godzinowy ujemny w
  trybie charge-only (DoD=0 lub block_discharge=True) w oknie 7-17, i
  toggle ładowania jest `on` (żeby nie blokować tego co jest już zablokowane).
  Blokada chroni przed ładowaniem baterii z sieci w drogiej taryfie.

- `should_block_battery_discharge` — aktywny:
  - **Pre-charge** (7:00 → `start_charge_hour_override`): hour-start reset
    + hysteresis 100/50 na `exported_energy_hourly`. Trzyma baterię gdy
    hour netto export > 100 Wh, odblokowuje gdy < 50 Wh.
  - **Post-charge** (`start_charge_hour_override` → 13:00): continuous
    check na `consumption_minus_pv_5_minutes` (sustained trend).
    Hysteresis -500/0 W. Unika cycling baterii (charge↔discharge) typowego
    dla post-charge phase gdzie charge_current>0.

Patrz `context/target_soc_algorithm.md` dla szerszego kontekstu.
"""

from __future__ import annotations

import logging
from typing import Final, Protocol

from custom_components.smart_rce.domain.input_state import InputState

_LOGGER = logging.getLogger(__name__)

# --- charge guard --- #

# Blokada ładowania baterii aktywna tylko dla hour < GUARD_END_HOUR — po tej
# godzinie brak PV, a tanie godziny RCE pozwalają na ładowanie baterii z sieci.
GUARD_END_HOUR: Final[int] = 17

# Hysteresis dla `hourly_balance_negative`: gdy flag=True, zostaje True
# dopóki eksport godzinowy nie przekroczy tego progu (anti-flap).
HYSTERESIS_WH: Final[int] = 50

# --- discharge guard (pre-charge window) --- #

# Pre-charge window start (hour).
PRE_CHARGE_WINDOW_START_HOUR: Final[int] = 7

# Hysteresis MINE dla block_discharge: set przy >=100 Wh, reset przy <50 Wh.
# Dead zone 50..100 zachowuje poprzedni stan.
DISCHARGE_HYSTERESIS_SET_WH: Final[int] = 100
DISCHARGE_HYSTERESIS_RESET_WH: Final[int] = 50

# --- discharge guard (post-charge window) --- #

# Post-charge window end (hour). Okno start_charge_hour_override → 13:00.
POST_CHARGE_WINDOW_END_HOUR: Final[int] = 13

# Continuous check na `consumption_minus_pv_5_minutes` (W):
# ujemne = PV > cons (surplus), dodatnie = cons > PV (deficit).
# Hysteresis wyzwala block_discharge gdy sustained surplus >=500W,
# resetuje gdy sustained deficit >0W. Dead zone -500..0 → keep state.
AVG_5MIN_SURPLUS_THRESHOLD_W: Final[int] = -500
AVG_5MIN_DEFICIT_THRESHOLD_W: Final[int] = 0


class BatteryState(Protocol):
    """Structural interface — minimalny kontrakt dla readers battery state.

    Używane przez WaterHeaterManager i inne managery które potrzebują
    orientacji czy bilans godzinowy jest ujemny.
    """

    hourly_balance_negative: bool


class BatteryManager:
    def __init__(self) -> None:
        self.hourly_balance_negative: bool = False
        self.should_block_battery_charge: bool = False
        self.should_block_battery_discharge: bool = False
        self._last_hour_seen: int | None = None

    def update(self, state: InputState) -> None:
        if self._none_present(state):
            _LOGGER.debug(
                "BatteryManager.update skipped (none_present): exported=%s now=%s",
                state.exported_energy_hourly,
                state.now,
            )
            return

        exported_energy_wh = state.exported_energy_hourly * 1000  # kWh → Wh

        # Snapshot poprzednich wartości dla detekcji transitions w logach
        prev_block_discharge = self.should_block_battery_discharge
        prev_block_charge = self.should_block_battery_charge
        prev_hourly_neg = self.hourly_balance_negative

        # --- OVERRIDE: intencjonalne rozładowanie (np. Battery Discharge Max) ---
        # Gdy input_boolean.ems_allow_discharge_override=True, EMS "stoi z boku".
        # Oba should_block_* wymuszone na False — pozwalamy innym automations
        # swobodnie sterować baterią bez interferencji.
        if state.ems_allow_discharge_override is True:
            self.should_block_battery_discharge = False
            self.should_block_battery_charge = False
            self.hourly_balance_negative = False
            self._last_hour_seen = None
            self._log_transitions(
                prev_block_discharge,
                prev_block_charge,
                prev_hourly_neg,
                reason="override_active",
            )
            _LOGGER.debug(
                "BatteryManager: OVERRIDE active — block_discharge=False, block_charge=False"
            )
            return

        # --- block_discharge ---
        if self._is_in_pre_charge_window(state):
            # Pre-charge: hour-start reset + hysteresis na exported_wh.
            if self._last_hour_seen != state.now.hour:
                self._last_hour_seen = state.now.hour
                self.should_block_battery_discharge = False
                _LOGGER.debug(
                    "BatteryManager[pre-charge]: hour-start reset (hour=%d) → "
                    "block_discharge=False",
                    state.now.hour,
                )
            elif exported_energy_wh >= DISCHARGE_HYSTERESIS_SET_WH:
                self.should_block_battery_discharge = True
            elif exported_energy_wh < DISCHARGE_HYSTERESIS_RESET_WH:
                self.should_block_battery_discharge = False
            # Dead zone 50..100 — zachowuje poprzedni stan
        elif self._is_in_post_charge_window(state):
            # Post-charge: continuous check na avg_5min (sustained trend).
            self._last_hour_seen = None
            avg_5min = state.consumption_minus_pv_5_minutes
            if avg_5min is None:
                self.should_block_battery_discharge = False
            elif avg_5min < AVG_5MIN_SURPLUS_THRESHOLD_W:
                self.should_block_battery_discharge = True
            elif avg_5min > AVG_5MIN_DEFICIT_THRESHOLD_W:
                self.should_block_battery_discharge = False
            # Dead zone -500..0 — zachowuje poprzedni stan
        else:
            # Poza obu oknami: reset.
            self.should_block_battery_discharge = False
            self._last_hour_seen = None

        # --- block_charge ---
        in_guard_window = (
            state.depth_of_discharge == 0 or self.should_block_battery_discharge
        ) and state.now.hour < GUARD_END_HOUR

        toggle_is_on = state.battery_charge_toggle_on is True

        if in_guard_window:
            if exported_energy_wh < 0 and toggle_is_on:
                self.hourly_balance_negative = True
            elif self.hourly_balance_negative and exported_energy_wh < HYSTERESIS_WH:
                pass  # hysteresis hold
            else:
                self.hourly_balance_negative = False
        else:
            self.hourly_balance_negative = False

        self.should_block_battery_charge = (
            self.hourly_balance_negative
            and state.battery_charge_limit is not None
            and state.battery_charge_limit >= 2
        )

        # --- Verbose debug co update (stan wejścia + wyjścia) ---
        phase = (
            "pre-charge"
            if self._is_in_pre_charge_window(state)
            else "post-charge"
            if self._is_in_post_charge_window(state)
            else "out-of-window"
        )
        _LOGGER.debug(
            "BatteryManager[%s] now=%s exported=%+.3fkWh(%+dWh) avg_5min=%s "
            "DoD=%s toggle=%s charge_limit=%s override_window=%s | "
            "block_discharge=%s block_charge=%s hourly_neg=%s",
            phase,
            state.now.strftime("%H:%M:%S") if state.now else "?",
            state.exported_energy_hourly,
            int(exported_energy_wh),
            f"{state.consumption_minus_pv_5_minutes:+.0f}W"
            if state.consumption_minus_pv_5_minutes is not None
            else "None",
            state.depth_of_discharge,
            state.battery_charge_toggle_on,
            state.battery_charge_limit,
            state.start_charge_hour_override,
            self.should_block_battery_discharge,
            self.should_block_battery_charge,
            self.hourly_balance_negative,
        )

        self._log_transitions(
            prev_block_discharge,
            prev_block_charge,
            prev_hourly_neg,
            reason=phase,
        )

    def _log_transitions(
        self,
        prev_block_discharge: bool,
        prev_block_charge: bool,
        prev_hourly_neg: bool,
        *,
        reason: str,
    ) -> None:
        """INFO-level logs dla transition events (łatwe grepowanie w logach)."""
        if prev_block_discharge != self.should_block_battery_discharge:
            _LOGGER.info(
                "BatteryManager: block_discharge %s → %s (reason: %s)",
                prev_block_discharge,
                self.should_block_battery_discharge,
                reason,
            )
        if prev_block_charge != self.should_block_battery_charge:
            _LOGGER.info(
                "BatteryManager: block_charge %s → %s (reason: %s)",
                prev_block_charge,
                self.should_block_battery_charge,
                reason,
            )
        if prev_hourly_neg != self.hourly_balance_negative:
            _LOGGER.debug(
                "BatteryManager: hourly_balance_negative %s → %s",
                prev_hourly_neg,
                self.hourly_balance_negative,
            )

    @staticmethod
    def _is_in_pre_charge_window(state: InputState) -> bool:
        """Pre-charge: 7:00 ≤ now < start_charge_hour_override (precyzja do minuty)."""
        if state.start_charge_hour_override is None or state.now is None:
            return False
        if state.now.hour < PRE_CHARGE_WINDOW_START_HOUR:
            return False
        # Porównanie time (hh:mm:ss) żeby obsłużyć override np. 10:30
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
    def _none_present(state: InputState) -> bool:
        return state.exported_energy_hourly is None or state.now is None
