"""Grid Export Manager — decyduje o EMS mode/Xset gdy bilans hourly POSITIVE.

Wystawia 4 pola czytane przez sensory:
- intervention_active (bool, diagnostic)
- recommended_ems_mode (str: "auto" | "battery_standby" | "charge_battery" | "buy_power")
- recommended_xset (int | None) — W
- last_decision_reason (str)

Listener w adapter.py reaguje na zmiany sensorów i wywołuje
number.goodwe_ems_power_limit + select.goodwe_ems_mode.

Active window: post_charge → next day 7:00 (skip pre_charge — tam
BatteryManager rządzi przez hysteresis 100/50 Wh + DoD).

Strategie (state machine):
- BATTERY_STANDBY  — gdy pv_power < 200W (noc, bateria stop)
- CHARGE_BATTERY   — Xset=6000W, bateria łapie surplus + import z grida
- BUY_POWER        — Xset=1500W, regulator trzyma meter ≈ 1500W import

Decision tree:
1. pv_power < 200W → STANDBY
2. battery_charge_limit ≤ 7A → CHARGE_BATTERY 6000 (BMS sam ograniczy charge)
3. battery_charge_limit > 7A → state machine na battery/meter intensity (27s mean window)

State machine używa "średniej intensywności w 27s" liczone jako
`-battery_power_avg_27s` (charging) i `-meter_active_power_total_avg_27s`
(import) — sensory HA mapują wartości UJEMNE (charging/import), my w
_apply_strategy konwertujemy na DODATNIE dla czytelności progów.

Progi:
- entry CHARGE: battery_charging_avg_27s > 2.5 kW (intensywne ładowanie sprzed)
- switch CHARGE→BUY: meter_import_avg_27s > 3.9 kW (za agresywny import)
- switch BUY→CHARGE: battery_charging_avg_27s > 4.9 kW (BMS cap blisko)

Hysteresis: window 27s mean (5-6 próbek @ scan_interval=5s) — mean rozprasza
wpływ single outliers z Goodwe Modbus + math thresholds (~0.9-1.1 kW gap
między progami CHARGE/BUY). Brak dodatkowego min-time-in-mode debouncingu.

Defensive: gdy battery_charge_limit lub avg_27s sensors są None (np. po HA
restart, sensory unavailable przez ~25-50ms) → no-op, manager wraca do AUTO,
listener wraca rejestry do AUTO. Po załadowaniu sensorów manager re-evaluuje.
"""

from __future__ import annotations

import logging
from typing import Final

from custom_components.smart_rce.domain.input_state import InputState

_LOGGER = logging.getLogger(__name__)


class GridExportManager:
    # POSITIVE balance gates
    # Entry > 0.06 (kompromis między YAML trigger 0.07 a condition 0.04)
    # Exit < 0.05 (jak YAML wait_template)
    # Deadzone 0.05-0.06 — akceptowalna oscylacja (max 2-3 cykle/godzinę przy
    # PV>>dom; utility_meter integral, brak spike'ów)
    POSITIVE_BALANCE_GATE_KWH: Final[float] = 0.06
    POSITIVE_EXIT_BALANCE_KWH: Final[float] = 0.05
    POSITIVE_SOC_CEILING: Final[int] = 100
    LATE_HOUR_MINUTE: Final[int] = 59
    LATE_HOUR_SECOND: Final[int] = 40
    EXIT_END_OF_HOUR_MINUTE: Final[int] = 59
    EXIT_END_OF_HOUR_SECOND: Final[int] = 50

    # Strategy thresholds (wartości DODATNIE — porównujemy do `_avg_27s`,
    # wyliczanych jako -battery_power_avg_27s i -meter_active_power_total_avg_27s).
    PV_STANDBY_THRESHOLD_W: Final[int] = 200
    BMS_LOW_LIMIT_A: Final[int] = 7  # battery_charge_limit ≤ 7A → "low BMS" branch
    BATTERY_CHARGING_INTENSE_W: Final[int] = (
        2500  # entry: bateria stale ≥ 2.5 kW charge → CHARGE
    )
    BATTERY_NEAR_BMS_CAP_W: Final[int] = (
        4900  # switch BUY→CHARGE gdy bateria stale ≥ 4.9 kW
    )
    METER_IMPORT_AGGRESSIVE_W: Final[int] = (
        3900  # switch CHARGE→BUY gdy meter stale ≥ 3.9 kW import
    )

    # Xset values per strategy (stałe — bez adaptacji)
    CHARGE_BATTERY_XSET_W: Final[int] = 6000
    BUY_POWER_XSET_W: Final[int] = 1500

    # Active window (skip pre_charge)
    PRE_CHARGE_WINDOW_START_HOUR: Final[int] = 7

    # Mode constants
    AUTO_MODE: Final[str] = "auto"
    STANDBY_MODE: Final[str] = "battery_standby"
    CHARGE_MODE: Final[str] = "charge_battery"
    BUY_POWER_MODE: Final[str] = "buy_power"

    # Strategy modes (input_select.smart_rce_grid_export_strategy_mode)
    STRATEGY_MODE_DISABLED: Final[str] = "disabled"
    STRATEGY_MODE_CHARGE_OR_STANDBY: Final[str] = "charge_or_standby"
    STRATEGY_MODE_ALL: Final[str] = "all"

    def __init__(self) -> None:
        self.intervention_active: bool = False
        self.recommended_ems_mode: str = self.AUTO_MODE
        self.recommended_xset: int | None = None
        self.last_decision_reason: str | None = None
        self._intervention_started_hour: int | None = None

    def update(self, state: InputState) -> None:
        """Re-evaluate intervention state from current InputState.

        Called reactively by Ems on every state change.
        """
        # 1. None-guard (core inputs)
        if self._none_present_core(state):
            self._set_neutral("none_present")
            return

        # 2. Active window — skip pre_charge (BatteryManager rządzi)
        if self._is_in_pre_charge_window(state):
            self._set_neutral("in_pre_charge_window")
            return

        # 3. Exit gates (gdy intervention_active)
        # Pierwsza gate: hour rollover — utility_meter resetuje balans hourly
        # o pełnej godzinie, każda godzina = osobna decyzja czy POSITIVE wystąpi.
        if self.intervention_active:
            if state.now.hour != self._intervention_started_hour:
                self._set_neutral("hour_rollover")
                self._apply_disabled_override_if_needed(state)
                return
            exit_reason = self._exit_reason(state)
            if exit_reason is not None:
                self._set_neutral(exit_reason)
                self._apply_disabled_override_if_needed(state)
                return
            # Continue active — re-evaluate strategy (może się zmienić)
            self._apply_strategy(state)
            self._apply_disabled_override_if_needed(state)
            return

        # 4. Entry gates (gdy not intervention_active)
        entry_block_reason = self._entry_block_reason(state)
        if entry_block_reason is not None:
            self._set_neutral(entry_block_reason)
            self._apply_disabled_override_if_needed(state)
            return

        # 5. Entry approved — start intervention
        self.intervention_active = True
        self._intervention_started_hour = state.now.hour
        self._apply_strategy(state)
        # 6. Final: jeśli disabled, override main outputs (intervention off),
        #    ale zachowaj would-be info w last_decision_reason
        self._apply_disabled_override_if_needed(state)

    def _apply_disabled_override_if_needed(self, state: InputState) -> None:
        """Jeśli strategy_mode = 'disabled' (lub None) → override main outputs.

        Manager już wystawił recommended_* (would-be decision). Teraz nadpisujemy
        na AUTO/None (intervention off, listener cofa Goodwe), zachowując
        diagnostykę w last_decision_reason.

        Defensive: None → traktuj jak "disabled" (safe default gdy helper
        jeszcze nieskonfigurowany).
        """
        mode = state.grid_export_strategy_mode
        if mode in (self.STRATEGY_MODE_CHARGE_OR_STANDBY, self.STRATEGY_MODE_ALL):
            return  # active mode — main outputs zostawiamy
        # mode is "disabled" or None — override
        would_be_mode = self.recommended_ems_mode
        would_be_xset = self.recommended_xset
        would_be_reason = self.last_decision_reason
        prefix = (
            "disabled" if mode == self.STRATEGY_MODE_DISABLED else "no_strategy_mode"
        )
        if would_be_mode == self.AUTO_MODE:
            self.last_decision_reason = f"{prefix} ({would_be_reason})"
        else:
            xset_str = f" {would_be_xset}W" if would_be_xset else ""
            self.last_decision_reason = (
                f"{prefix} (would: {would_be_mode}{xset_str}, {would_be_reason})"
            )
        self.intervention_active = False
        self.recommended_ems_mode = self.AUTO_MODE
        self.recommended_xset = None

    # --- gates ---

    @staticmethod
    def _none_present_core(state: InputState) -> bool:
        """Pola wymagane do podjęcia DECYZJI o entry/exit (gates)."""
        return (
            state.now is None
            or state.exported_energy_hourly is None
            or state.battery_soc is None
            or state.pv_power is None
        )

    @staticmethod
    def _none_present_high_bms_machine(state: InputState) -> bool:
        """Pola wymagane do high-BMS state machine (CHARGE_BATTERY ↔ BUY_POWER).

        Nie wymagane dla low-BMS branch (CHARGE 6000 fallback).
        """
        return (
            state.battery_power_avg_27s is None
            or state.meter_active_power_total_avg_27s is None
        )

    @classmethod
    def _is_in_pre_charge_window(cls, state: InputState) -> bool:
        """Pre-charge: 7:00 ≤ now < start_charge_hour_override."""
        if state.start_charge_hour_override is None:
            return False
        if state.now.hour < cls.PRE_CHARGE_WINDOW_START_HOUR:
            return False
        return state.now.time() < state.start_charge_hour_override

    @classmethod
    def _exit_reason(cls, state: InputState) -> str | None:
        """Return exit reason if any exit gate fires, else None."""
        if state.exported_energy_hourly < cls.POSITIVE_EXIT_BALANCE_KWH:
            return "balance_recovered"
        if state.battery_soc >= cls.POSITIVE_SOC_CEILING:
            return "soc_ceiling_exit"
        if state.battery_charge_toggle_on is False:
            return "toggle_off_exit"
        if (
            state.now.minute >= cls.EXIT_END_OF_HOUR_MINUTE
            and state.now.second >= cls.EXIT_END_OF_HOUR_SECOND
        ):
            return "end_of_hour_cleanup"
        return None

    @classmethod
    def _entry_block_reason(cls, state: InputState) -> str | None:
        """Return reason if entry blocked, else None (entry allowed)."""
        if state.exported_energy_hourly <= cls.POSITIVE_BALANCE_GATE_KWH:
            return "balance_below_threshold"
        if state.battery_soc >= cls.POSITIVE_SOC_CEILING:
            return "soc_at_ceiling"
        if state.battery_charge_toggle_on is False:
            return "toggle_off"
        if not (
            state.now.minute < cls.LATE_HOUR_MINUTE
            or state.now.second < cls.LATE_HOUR_SECOND
        ):
            return "too_late_in_hour"
        if state.other_ems_automation_active_this_hour is True:
            return "other_automation_active"
        return None

    # --- strategy ---

    def _apply_strategy(self, state: InputState) -> None:
        """Pick STANDBY / CHARGE_BATTERY / BUY_POWER and set outputs.

        State machine używa `self.recommended_ems_mode` jako current_mode.
        """
        # 1. PV niskie → STANDBY (najwyższy priorytet, nawet w trakcie active)
        if state.pv_power < self.PV_STANDBY_THRESHOLD_W:
            self.recommended_ems_mode = self.STANDBY_MODE
            self.recommended_xset = None
            self.last_decision_reason = "low_pv_standby"
            return

        # 2. charge_or_standby mode — CHARGE_BATTERY 6000 force, bez BUY_POWER
        # (PV<200 już obsłużone w kroku 1 — STANDBY)
        if state.grid_export_strategy_mode == self.STRATEGY_MODE_CHARGE_OR_STANDBY:
            self.recommended_ems_mode = self.CHARGE_MODE
            self.recommended_xset = self.CHARGE_BATTERY_XSET_W
            self.last_decision_reason = "charge_or_standby_force_charge"
            return

        # 3. battery_charge_limit None → defensive, czekamy na sensor
        if state.battery_charge_limit is None:
            self._set_neutral("none_battery_charge_limit")
            return

        # 4. Low BMS branch — CHARGE_BATTERY 6000, bez state machine
        # (nie wymaga battery_power_avg_27s ani meter_avg_27s)
        if state.battery_charge_limit <= self.BMS_LOW_LIMIT_A:
            self.recommended_ems_mode = self.CHARGE_MODE
            self.recommended_xset = self.CHARGE_BATTERY_XSET_W
            self.last_decision_reason = "low_bms_charge"
            return

        # 5. High BMS branch — wymaga avg_27s sensors
        if self._none_present_high_bms_machine(state):
            self._set_neutral("none_present_high_bms_machine")
            return

        # State machine (CHARGE_BATTERY ↔ BUY_POWER)
        # Konwersja na wartości dodatnie dla czytelnych progów:
        # - battery_power: ujemne = charging → battery_charging_avg_27s = minimum mocy ładowania
        # - meter_active_power: ujemne = import → meter_import_avg_27s = minimum mocy importu
        # max(ujemne) = wartość najbliższa zera = NAJMNIEJSZA intensywność,
        # więc -max(ujemne) = minimum INTENSYWNOŚCI (charging/import) w oknie 18s.
        battery_charging_avg_27s = -state.battery_power_avg_27s
        meter_import_avg_27s = -state.meter_active_power_total_avg_27s
        current = self.recommended_ems_mode

        if current == self.CHARGE_MODE:
            # Wyjście z CHARGE: gdy importujemy stale ≥ 3.9 kW (za agresywne)
            if meter_import_avg_27s > self.METER_IMPORT_AGGRESSIVE_W:
                self.recommended_ems_mode = self.BUY_POWER_MODE
                self.recommended_xset = self.BUY_POWER_XSET_W
                self.last_decision_reason = "switch_charge_to_buy_meter_aggressive"
            else:
                self.recommended_xset = self.CHARGE_BATTERY_XSET_W
                self.last_decision_reason = "stay_charge_battery"
        elif current == self.BUY_POWER_MODE:
            # Wyjście z BUY: gdy bateria stale ≥ 4.9 kW (BMS cap blisko, PV cięcie zaraz)
            if battery_charging_avg_27s > self.BATTERY_NEAR_BMS_CAP_W:
                self.recommended_ems_mode = self.CHARGE_MODE
                self.recommended_xset = self.CHARGE_BATTERY_XSET_W
                self.last_decision_reason = "switch_buy_to_charge_near_bms_cap"
            else:
                self.recommended_xset = self.BUY_POWER_XSET_W
                self.last_decision_reason = "stay_buy_power"
        elif battery_charging_avg_27s > self.BATTERY_CHARGING_INTENSE_W:
            self.recommended_ems_mode = self.CHARGE_MODE
            self.recommended_xset = self.CHARGE_BATTERY_XSET_W
            self.last_decision_reason = "entry_charge_intense_charging"
        else:
            self.recommended_ems_mode = self.BUY_POWER_MODE
            self.recommended_xset = self.BUY_POWER_XSET_W
            self.last_decision_reason = "entry_buy_power_default"

    def _set_neutral(self, reason: str) -> None:
        """Reset to AUTO mode with given reason. Idempotent."""
        self.intervention_active = False
        self.recommended_ems_mode = self.AUTO_MODE
        self.recommended_xset = None
        self.last_decision_reason = reason
        self._intervention_started_hour = None
