"""Battery charge/discharge management.

Monitoruje bilans godzinowy (`exported_energy_hourly`) i decyduje kiedy
zablokować ładowanie/rozładowywanie baterii.

Decyzje:

- `should_block_battery_charge` — aktywny gdy bilans godzinowy ujemny w
  trybie charge-only (DoD=0 lub block_discharge=True) w oknie 7-17, i
  toggle ładowania jest `on` (żeby nie blokować tego co jest już zablokowane).
  Blokada chroni przed ładowaniem baterii z sieci w drogiej taryfie.

- `should_block_battery_discharge` — aktywny w pre-charge window
  (7:00 → `start_charge_hour_override`) gdy eksport godzinowy netto
  przekroczy próg. Trzyma baterię (DoD=0) żeby nie rozładowywała się
  przy chwilowych deficytach, które i tak zbilansują się w netto
  rozliczeniu godzinowym. Patrz `context/target_soc_algorithm.md`.

Post-charge logic (okno start_charge → 13:00) to-do — osobna iteracja.
"""

from __future__ import annotations

from typing import Final, Protocol

from custom_components.smart_rce.domain.input_state import InputState

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
            return

        exported_energy_wh = state.exported_energy_hourly * 1000  # kWh → Wh

        # --- block_discharge (pre-charge window only) ---
        if self._is_in_pre_charge_window(state):
            # Hour-start reset: każda nowa godzina zaczyna od block_discharge=False.
            # Hysteresis w ramach godziny dalej decyduje na podstawie exported_wh.
            if self._last_hour_seen != state.now.hour:
                self._last_hour_seen = state.now.hour
                self.should_block_battery_discharge = False
            elif exported_energy_wh >= DISCHARGE_HYSTERESIS_SET_WH:
                self.should_block_battery_discharge = True
            elif exported_energy_wh < DISCHARGE_HYSTERESIS_RESET_WH:
                self.should_block_battery_discharge = False
                # Dead zone 50..100 — zachowuje poprzedni stan
        else:
            # Poza pre-charge: reset. Post-charge logic to-do.
            self.should_block_battery_discharge = False
            self._last_hour_seen = None

        # --- block_charge ---
        # in_guard_window = (DoD==0 OR block_discharge) AND hour<GUARD_END_HOUR
        # OR z DoD zachowane bo po 13:00 (koniec pre-charge) chcemy żeby
        # block_charge nadal działał gdy DoD=0 (ustawione przez inne automation).
        in_guard_window = (
            state.depth_of_discharge == 0 or self.should_block_battery_discharge
        ) and state.now.hour < GUARD_END_HOUR

        # Toggle guard: nie ustawiaj hourly_balance_negative=True jeśli toggle=False
        # (bateria już jest zablokowana od ładowania — block_charge redundant).
        toggle_is_on = state.battery_charge_toggle_on is True

        if in_guard_window:
            if exported_energy_wh < 0 and toggle_is_on:
                self.hourly_balance_negative = True
            elif self.hourly_balance_negative and exported_energy_wh < HYSTERESIS_WH:
                # hysteresis — zostaje True dopóki nie ma solidnego eksportu
                pass
            else:
                self.hourly_balance_negative = False
        else:
            self.hourly_balance_negative = False

        self.should_block_battery_charge = (
            self.hourly_balance_negative
            and state.battery_charge_limit is not None
            and state.battery_charge_limit >= 2
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
    def _none_present(state: InputState) -> bool:
        return state.exported_energy_hourly is None or state.now is None
