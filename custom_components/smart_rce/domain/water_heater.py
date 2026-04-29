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

from datetime import datetime

from custom_components.smart_rce.domain.battery import BatteryState
from custom_components.smart_rce.domain.input_state import InputState

# BALANCED Piętro 2 — adaptacyjny upgrade pod budżet eksportu w resztę godziny.
# Bonus = exported_energy_so_far / czas_do_końca_godziny — przelicza dotąd
# wyeksportowane Wh na ekwiwalent dodatkowej mocy dostępnej do dożarcia.
EXPORT_BONUS_CUTOFF_SEC: int = 60  # < tego nie aktywuj bonusa (ostatnia minuta)
EXPORT_BONUS_MIN_T_LEFT_SEC: int = 60  # clamp dolny dla dzielenia
EXPORT_BONUS_HYSTERESIS_W: int = 500  # symmetryczny do Piętra 1


def seconds_until_hour_end(now: datetime) -> int:
    return 3600 - (now.minute * 60 + now.second)


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

    # Krótkie etykiety do display sensorów
    _STATE_LABELS: dict[str, str] = {
        "both_are_off": "off",
        "small_is_on": "small",
        "big_is_on": "big",
        "both_are_on": "both",
    }

    def __init__(self) -> None:
        self.should_turn_on: bool = False
        self.should_turn_off: bool = False
        self.should_turn_on_small: bool = False
        self.should_turn_off_small: bool = False
        # BALANCED diagnostics
        self.balanced_heater_budget: float | None = None
        self.balanced_baseline: str | None = None
        self.balanced_upgrade_target: str | None = None
        self.balanced_upgrade_active: bool = False
        self.balanced_export_bonus_w: float | None = None

    def update(
        self,
        state: InputState,
        battery: BatteryState,
        grid_export=None,  # noqa: ANN001 — duck-typed (GridExportManager-like)
    ) -> None:
        """Update target state based on PV/battery/heater config.

        `grid_export` (optional, default None for backward compat) — gdy
        recommended_ems_mode == "charge_battery", reserved budget zostaje
        zwiększony do 3500W (chronimy baterię intervention przed konkurencją
        z grzałkami). `mode=buy_power` / `auto` / `battery_standby` →
        original logic (regulator dynamic, grzałki działają normalnie).
        """
        if self._none_present(state):
            return

        current_state = self._current_state(state)
        target = self._determine_target(state, battery, current_state, grid_export)

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
            self.balanced_upgrade_target = None
            self.balanced_upgrade_active = False
            self.balanced_export_bonus_w = None
        elif battery.hourly_balance_negative:
            pv_available = -state.consumption_minus_pv_2_minutes
            self.balanced_heater_budget = -pv_available
            self.balanced_baseline = "negative_energy"
            self.balanced_upgrade_target = None
            self.balanced_upgrade_active = False
            self.balanced_export_bonus_w = None

    def _current_state(self, state: InputState) -> str:
        if state.water_heater_big_is_on and state.water_heater_small_is_on:
            return self.BOTH_ARE_ON
        if state.water_heater_big_is_on:
            return self.BIG_IS_ON
        if state.water_heater_small_is_on:
            return self.SMALL_IS_ON
        return self.BOTH_ARE_OFF

    def _determine_target(
        self,
        state: InputState,
        battery: BatteryState,
        current_state: str,
        grid_export=None,  # noqa: ANN001 — duck-typed
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

        # GridExport intervention CHARGE_BATTERY → priorytet baterii, większa
        # rezerwacja PV (chronimy przed konkurencją grzałek). buy_power /
        # battery_standby / auto → original logic.
        grid_export_charge_active = (
            grid_export is not None
            and getattr(grid_export, "recommended_ems_mode", None) == "charge_battery"
        )

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
                state.now,
                grid_export_charge_active,
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
        now: datetime,
        grid_export_charge_active: bool = False,
    ) -> str:
        # Rezerwacja (charge_limit: dyskretne 0, 1, 2, 7, 18A)
        # BATTERY_FIRST: gdy bateria może mocno ładować (charge_limit>7),
        # rezerwujemy pełne 4500W dla baterii, grzałki praktycznie OFF. Gdy
        # bateria zbliża się do pełna, charge_limit sam spada (7→2→1→0) i
        # fallback do istniejącej "łaskawej" logiki.
        # GridExport CHARGE intervention (charge_battery active) — bateria
        # ma priorytet, reserved=3500 (jak BATTERY_FIRST ale low BMS branchy
        # bez zmian). Konkurencja z grzałkami eliminowana — typowo SMALL/BIG
        # włącza się dopiero gdy pv_available > 5000/6500.
        if strategy == "BATTERY_FIRST" and battery_charge_limit > 7:
            reserved = 4500
        elif battery_charge_limit > 7:
            if grid_export_charge_active:
                reserved = 3500
            else:
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

        # Piętro 2 — adaptacyjny upgrade pod budżet eksportu w resztę godziny.
        # Skip gdy battery_charge_limit > 7 (bateria chce max mocy ~5.2 kW).
        # Instalacja PV 9.1 kW, grzałki max 4.5 kW, bateria max 5.2 kW — suma
        # 9.7 kW > PV. Gdy bateria chce max, każdy włączony watt grzałki jest
        # zabrany baterii (Goodwe rebalansuje automatycznie).
        #
        # Bonus: ile dodatkowej mocy "musi być zjedzone" w resztę godziny,
        # żeby zniwelować dotąd wyeksportowane Wh. Cap na BOTH_POWER — nigdy
        # nie udajemy że mamy więcej dyspozycyjnej mocy niż zjedzą obie grzałki.
        # W ostatnich EXPORT_BONUS_CUTOFF_SEC sekundach nie aktywujemy bonusa
        # (i tak nie zdążymy zjeść; uniknięcie ostatniego szarpnięcia przed
        # resetem utility_meter).
        skip_upgrade = battery_charge_limit > 7
        seconds_left = seconds_until_hour_end(now)
        if seconds_left >= EXPORT_BONUS_CUTOFF_SEC and not skip_upgrade:
            t_left_h = max(seconds_left, EXPORT_BONUS_MIN_T_LEFT_SEC) / 3600
            export_bonus_w = min(
                float(self.BOTH_POWER), max(0.0, exported_energy / t_left_h)
            )
        else:
            export_bonus_w = 0.0

        effective_budget = heater_budget + export_bonus_w
        h = EXPORT_BONUS_HYSTERESIS_W

        # Drabinka identyczna do baseline, ale na effective_budget — wybiera
        # NAJWYŻSZY stan mieszczący się w budżecie (skok N→N+2 dozwolony).
        if effective_budget >= self.BOTH_POWER or (
            effective_budget >= self.BOTH_POWER - h
            and current_state == self.BOTH_ARE_ON
        ):
            upgrade_candidate = self.BOTH_ARE_ON
        elif effective_budget >= self.BIG_POWER or (
            effective_budget >= self.BIG_POWER - h and current_state == self.BIG_IS_ON
        ):
            upgrade_candidate = self.BIG_IS_ON
        elif effective_budget >= self.SMALL_POWER or (
            effective_budget >= self.SMALL_POWER - h
            and current_state == self.SMALL_IS_ON
        ):
            upgrade_candidate = self.SMALL_IS_ON
        else:
            upgrade_candidate = self.BOTH_ARE_OFF

        # target = max(baseline, upgrade_candidate) — Piętro 2 nigdy nie schodzi
        # poniżej baseline (baseline ma swoją histerezę na samym pv).
        if self._STATE_ORDER[upgrade_candidate] > self._STATE_ORDER[baseline]:
            target = upgrade_candidate
        else:
            target = baseline

        # Diagnostyka
        self.balanced_heater_budget = -heater_budget
        self.balanced_baseline = baseline
        self.balanced_upgrade_active = target != baseline
        if target == baseline:
            # Baseline sam pokrywa target — upgrade niepotrzebny.
            self.balanced_upgrade_target = f"{self._STATE_LABELS[baseline]} (baseline)"
        else:
            # Adaptacyjny upgrade: pokazujemy "from -> to" żeby w wykresie
            # widać było skok N→N+1, N→N+2 lub N→N+3.
            self.balanced_upgrade_target = (
                f"{self._STATE_LABELS[baseline]} -> {self._STATE_LABELS[target]}"
            )
        self.balanced_export_bonus_w = export_bonus_w

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
