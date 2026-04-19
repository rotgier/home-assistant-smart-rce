"""Battery charge/discharge management.

Monitoruje bilans godzinowy (`exported_energy_hourly`) i decyduje kiedy
zablokować ładowanie/rozładowywanie baterii. Obecnie:

- `should_block_battery_charge` — aktywny gdy bilans godzinowy ujemny
  w trybie charge-only (DoD=0) w oknie 7-17. Blokada chroni przed
  ładowaniem baterii z sieci w drogiej taryfie.

Przygotowane miejsce na przyszłą logikę dynamic DoD switching
(blokada rozładowania gdy bilans godzinowy dodatni — trzymaj baterię
na droższe godziny). Patrz `context/target_soc_algorithm.md`.
"""

from __future__ import annotations

from typing import Final, Protocol

from custom_components.smart_rce.domain.input_state import InputState

# Blokadaada ładowania baterii aktywna tylko dla hour < GUARD_END_HOUR — po tej godzinie
# brak PV, a tanie godziny RCE pozwalają na ładowanie baterii z sieci.
GUARD_END_HOUR: Final[int] = 17

# Hysteresis dla `hourly_balance_negative`: gdy flag=True, zostaje True
# dopóki eksport godzinowy nie przekroczy tego progu (anti-flap).
HYSTERESIS_WH: Final[int] = 50


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

    def update(self, state: InputState) -> None:
        if self._none_present(state):
            return

        exported_energy_wh = state.exported_energy_hourly * 1000  # kWh → Wh

        # Guard window: charge-only mode (DoD=0) w godzinach PV.
        # Poza guardem — flag zawsze False (bilans godzinowy nie jest
        # istotny poza oknem gdzie chronimy ładowanie z sieci).
        in_guard_window = (
            state.depth_of_discharge is not None
            and state.depth_of_discharge == 0
            and state.now.hour < GUARD_END_HOUR
        )

        if in_guard_window:
            if exported_energy_wh < 0:
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
    def _none_present(state: InputState) -> bool:
        return state.exported_energy_hourly is None or state.now is None
