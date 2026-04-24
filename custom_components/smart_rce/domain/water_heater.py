"""Water Heater Manager — CWU heater on/off decisions.

Sterowanie grzałkami CWU (BIG 3kW, SMALL 1.5kW) na podstawie:
- PV surplus (sensor minus_pv)
- SOC baterii + battery_charge_limit (ile bateria przyjmie)
- Bilansu godzinowego eksport/import (przez BatteryState)
- Mode: ASAP / BALANCED / WASTED

Battery-related decisions (block charge/discharge) są w BatteryManager —
ten Manager tylko *czyta* battery.hourly_balance_negative jako guard.
"""

from __future__ import annotations

from custom_components.smart_rce.domain.battery import BatteryState
from custom_components.smart_rce.domain.input_state import InputState


class WaterHeaterManager:
    BIG_POWER: int = 3000
    SMALL_POWER: int = 1500
    BOTH_POWER: int = 4500
    BATTERY_VOLTAGE: int = 290

    BOTH_ARE_ON: str = "both_are_on"
    BIG_IS_ON: str = "big_is_on"
    SMALL_IS_ON: str = "small_is_on"
    BOTH_ARE_OFF: str = "both_are_off"

    # Hierarchia stanów do porównania
    _STATE_ORDER: dict[str, int] = {
        "both_are_off": 0,
        "small_is_on": 1,
        "big_is_on": 2,
        "both_are_on": 3,
    }

    _UPGRADE_MAP: dict[str, str] = {
        "both_are_off": "small_is_on",
        "small_is_on": "big_is_on",
        "big_is_on": "both_are_on",
        "both_are_on": "both_are_on",
    }

    def __init__(self) -> None:
        self.should_turn_on: bool = False
        self.should_turn_off: bool = False
        self.should_turn_on_small: bool = False
        self.should_turn_off_small: bool = False
        # BALANCED diagnostics
        self.balanced_heater_budget: float | None = None
        self.balanced_baseline: str | None = None
        self.balanced_upgrade_active: bool = False

    def update(self, state: InputState, battery: BatteryState) -> None:
        if self._none_present(state):
            return

        current_state = self._current_state(state)
        target = self._determine_target(state, battery, current_state)

        self.should_turn_on = target in (self.BIG_IS_ON, self.BOTH_ARE_ON)
        self.should_turn_off = target in (self.SMALL_IS_ON, self.BOTH_ARE_OFF)
        self.should_turn_on_small = target in (self.SMALL_IS_ON, self.BOTH_ARE_ON)
        self.should_turn_off_small = target in (self.BIG_IS_ON, self.BOTH_ARE_OFF)

        # BALANCED diagnostics — gdy bilans godzinowy ujemny, budżet jest
        # ujemną nadwyżką (import netto), pokazujemy to jako diagnostykę.
        mode = state.heater_mode or "BALANCED"
        if mode != "BALANCED":
            self.balanced_heater_budget = None
            self.balanced_baseline = None
            self.balanced_upgrade_active = False
        elif battery.hourly_balance_negative:
            pv_available = -state.consumption_minus_pv_2_minutes
            self.balanced_heater_budget = -pv_available
            self.balanced_baseline = "negative_energy"
            self.balanced_upgrade_active = False

    def _current_state(self, state: InputState) -> str:
        if state.water_heater_big_is_on and state.water_heater_small_is_on:
            return self.BOTH_ARE_ON
        if state.water_heater_big_is_on:
            return self.BIG_IS_ON
        if state.water_heater_small_is_on:
            return self.SMALL_IS_ON
        return self.BOTH_ARE_OFF

    def _determine_target(
        self, state: InputState, battery: BatteryState, current_state: str
    ) -> str:
        pv_available = -state.consumption_minus_pv_2_minutes
        battery_soc = state.battery_soc
        battery_charge_limit = state.battery_charge_limit
        exported_energy = state.exported_energy_hourly * 1000  # kWh → Wh

        # GUARD: Ochrona bilansu godzinowego (tryb charge-only, DoD=0%) — tylko
        # w godzinach PV. Zarządzany przez BatteryManager (hysteresis, guard window).
        if battery.hourly_balance_negative:
            return self.BOTH_ARE_OFF

        mode = state.heater_mode or "BALANCED"

        if mode == "ASAP":
            target = self._asap_target(
                pv_available, battery_charge_limit, current_state
            )
        elif mode == "BALANCED":
            return self._balanced_target(
                pv_available,
                battery_charge_limit,
                battery_soc,
                exported_energy,
                current_state,
                state.water_heater_strategy,
            )
        else:
            target = self._wasted_target(
                pv_available, battery_charge_limit, current_state
            )

        # Override: exported_energy — nie marnuj skumulowanego eksportu
        # (tylko dla ASAP i WASTED, NIE dla BALANCED)
        if battery_soc >= 90:
            if exported_energy > 300 and pv_available > 0:
                if target in (self.BOTH_ARE_OFF, self.SMALL_IS_ON):
                    target = self.BIG_IS_ON

            if exported_energy > 80:
                if self._STATE_ORDER[current_state] > self._STATE_ORDER[target]:
                    target = current_state

        return target

    def _asap_target(
        self, pv: float, battery_charge_limit: float, current_state: str
    ) -> str:
        battery_full = battery_charge_limit == 0
        thresholds = (1500, 3000, 4500) if battery_full else (1800, 3300, 4800)
        hysteresis = 500

        if pv > thresholds[2] or (
            pv > thresholds[2] - hysteresis and current_state == self.BOTH_ARE_ON
        ):
            return self.BOTH_ARE_ON
        if pv > thresholds[1] or (
            pv > thresholds[1] - hysteresis
            and current_state in (self.BIG_IS_ON, self.BOTH_ARE_ON)
        ):
            return self.BIG_IS_ON
        if pv > thresholds[0] or (
            pv > thresholds[0] - hysteresis
            and current_state in (self.SMALL_IS_ON, self.BIG_IS_ON, self.BOTH_ARE_ON)
        ):
            return self.SMALL_IS_ON
        return self.BOTH_ARE_OFF

    def _wasted_target(
        self, pv: float, battery_charge_limit: float, current_state: str
    ) -> str:
        battery_max_charge = battery_charge_limit * self.BATTERY_VOLTAGE
        pv_surplus = pv - battery_max_charge
        hysteresis = 500

        # pv_surplus nie zależy od stanu grzałek (sensor minus_heaters)
        # Step-up: OFF → BIG → BOTH (small nigdy sam w WASTED)
        if pv_surplus > self.BIG_POWER or (
            pv_surplus > self.BIG_POWER - hysteresis
            and current_state == self.BOTH_ARE_ON
        ):
            return self.BOTH_ARE_ON
        if pv_surplus > 0 or (
            pv_surplus > -hysteresis
            and current_state in (self.BIG_IS_ON, self.BOTH_ARE_ON)
        ):
            return self.BIG_IS_ON
        return self.BOTH_ARE_OFF

    def _balanced_target(
        self,
        pv: float,
        battery_charge_limit: float,
        battery_soc: float,
        exported_energy: float,
        current_state: str,
        strategy: str | None,
    ) -> str:
        # Rezerwacja (charge_limit: dyskretne 0, 1, 2, 7, 18A)
        # BATTERY_FIRST: gdy bateria może mocno ładować (charge_limit>7),
        # rezerwujemy pełne 4500W dla baterii, grzałki praktycznie OFF. Gdy
        # bateria zbliża się do pełna, charge_limit sam spada (7→2→1→0) i
        # fallback do istniejącej "łaskawej" logiki.
        if strategy == "BATTERY_FIRST" and battery_charge_limit > 7:
            reserved = 4500
        elif battery_charge_limit > 7:
            reserved = 3500 if battery_soc < 50 else 2500
        elif battery_charge_limit > 2:
            reserved = 1000
        elif battery_charge_limit == 2:
            reserved = 300
        else:
            reserved = 0

        heater_budget = pv - reserved
        hysteresis = 500

        # Piętro 1 — Baseline (histereza trzyma tylko obecny stan, nie wyższy)
        if heater_budget >= self.BOTH_POWER or (
            heater_budget >= self.BOTH_POWER - hysteresis
            and current_state == self.BOTH_ARE_ON
        ):
            baseline = self.BOTH_ARE_ON
        elif heater_budget >= self.BIG_POWER or (
            heater_budget >= self.BIG_POWER - hysteresis
            and current_state == self.BIG_IS_ON
        ):
            baseline = self.BIG_IS_ON
        elif heater_budget >= self.SMALL_POWER or (
            heater_budget >= self.SMALL_POWER - hysteresis
            and current_state == self.SMALL_IS_ON
        ):
            baseline = self.SMALL_IS_ON
        else:
            baseline = self.BOTH_ARE_OFF

        # Piętro 2 — Upgrade z budżetu eksportu godzinowego.
        # Skip gdy battery_charge_limit > 7 (bateria chce max mocy ~5.2 kW).
        # Instalacja PV 9.1 kW, grzałki max 4.5 kW, bateria max 5.2 kW — suma
        # 9.7 kW > PV. Gdy bateria chce max, każdy włączony watt grzałki jest
        # zabrany baterii (Goodwe rebalansuje automatycznie). Cumulative
        # exported_energy > 100 Wh to historia z wcześniejszych minut godziny,
        # nie znak bieżącego surplus. Universal (niezależne od strategy),
        # symetryczne do warunku na Piętrze 1 BATTERY_FIRST.
        upgrade = self._UPGRADE_MAP[baseline]
        target = baseline
        skip_upgrade = battery_charge_limit > 7
        if upgrade != baseline and not skip_upgrade:
            if exported_energy > 100 or (
                self._STATE_ORDER[current_state] >= self._STATE_ORDER[upgrade]
                and exported_energy > 30
            ):
                target = upgrade

        # Diagnostyka
        self.balanced_heater_budget = -heater_budget
        self.balanced_baseline = baseline
        self.balanced_upgrade_active = target != baseline

        return target

    def _none_present(self, state: InputState) -> bool:
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
