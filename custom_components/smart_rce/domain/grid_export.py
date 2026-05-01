"""Grid Export Manager — decyduje o EMS mode/Xset gdy bilans hourly POSITIVE.

Wystawia 4 pola czytane przez sensory:
- intervention_active (bool, diagnostic)
- recommended_ems_mode (str: "auto" | "discharge_battery" | "charge_battery")
- recommended_xset (int | None) — W
- last_decision_reason (str)

Listener w adapter.py reaguje na zmiany sensorów i wywołuje
number.goodwe_ems_power_limit + select.goodwe_ems_mode.

Active window: post_charge → next day 7:00 (skip pre_charge — tam
BatteryManager rządzi przez hysteresis 100/50 Wh + DoD).

Strategie:
- STANDBY (discharge_battery xset=0) — gdy pv_power_avg_2_minutes < 200W
                                       (noc, bateria target=0, house z grida)
- CHARGE_BATTERY adaptive            — Xset z lookup table na pv_available
                                       (-consumption_minus_pv_2_minutes);
                                       pv_avail ≤ -1000 → AUTO

Decision tree (`grid_export_strategy_mode`):
- "charge_adaptive" → domyślne aktywne (STANDBY lub adaptive Xset)
- "disabled"        → manager evaluuje, ale intervention off (diagnostic only)

Hysteresis: w charge_adaptive lookup — current Xset stable jeśli pv_available
mieści się w rozszerzonym range (±300W od bucket boundaries). Eliminuje
flap'owanie Xset gdy pv_available oscyluje na granicy.

Defensive: gdy `consumption_minus_pv_2_minutes` lub `battery_charge_limit`
są None (np. po HA restart, sensory unavailable przez ~25-50ms) → no-op,
manager wraca do AUTO, listener wraca rejestry do AUTO.
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

    # Strategy thresholds
    PV_STANDBY_THRESHOLD_W: Final[int] = 200
    BMS_LOW_LIMIT_A: Final[int] = 7  # battery_charge_limit ≤ 7A → low BMS shortcut

    # Active window (skip pre_charge)
    PRE_CHARGE_WINDOW_START_HOUR: Final[int] = 7

    # Mode constants
    AUTO_MODE: Final[str] = "auto"
    # Semantyczne "standby" (bateria stoi). Faktyczny Goodwe mode = discharge_battery
    # z Xset=0 — bateria target=0W (stoi), house consumption idzie z grida →
    # import zjada saldo POSITIVE. Zastąpił literalny battery_standby — tamten
    # w praktyce dopuszczał faktyczny discharge (obs. 2026-04-30 22:48
    # battery_power=-1300/-1400W mimo battery_standby).
    STANDBY_MODE: Final[str] = "discharge_battery"
    CHARGE_MODE: Final[str] = "charge_battery"

    # Strategy modes (input_select.smart_rce_grid_export_strategy_mode)
    STRATEGY_MODE_DISABLED: Final[str] = "disabled"
    STRATEGY_MODE_CHARGE_ADAPTIVE: Final[str] = "charge_adaptive"

    # Adaptive charge lookup — pv_available (= -consumption_minus_pv_2_minutes)
    # mapped na Xset. Każdy próg w (W). Pierwszy spełniający → Xset.
    # pv_available ≤ -1000 → AUTO (block_discharge w battery.py przejmuje).
    CHARGE_ADAPTIVE_BUCKETS: Final[tuple[tuple[int, int], ...]] = (
        (4000, 6000),
        (3000, 5000),
        (2000, 4000),
        (1000, 3000),
        (0, 2000),
        (-1000, 1000),
    )
    # Low BMS shortcut dla charge_adaptive — gdy BMS clamp na ~2 kW (charge_limit
    # ≤ 7A), nie ma sensu obliczać Xset z lookup. Stałe 3500 W (BMS i tak
    # ograniczy charge do ~2 kW, +1.5 kW margines).
    CHARGE_ADAPTIVE_LOW_BMS_XSET_W: Final[int] = 3500

    # Hysteresis dla charge_adaptive — gdy current Xset jest w lookup, sprawdź
    # czy pv_available mieści się w rozszerzonym range (±300W od granic bucket'a).
    # Eliminuje flap'owanie Xset gdy pv_available oscyluje na granicy bucket'a.
    # Pierwszy tick intervention (current_xset=None) → bez hysteresis (lookup).
    CHARGE_ADAPTIVE_HYSTERESIS_W: Final[int] = 300

    # Logowanie — throttle DEBUG snapshot gdy nic się nie zmienia (60s).
    DEBUG_LOG_THROTTLE_SEC: Final[int] = 60

    def __init__(self) -> None:
        self.intervention_active: bool = False
        self.recommended_ems_mode: str = self.AUTO_MODE
        self.recommended_xset: int | None = None
        self.last_decision_reason: str | None = None
        self._intervention_started_hour: int | None = None
        # Throttling dla DEBUG snapshotów (jak w BatteryManager)
        self._last_log_snapshot: tuple | None = None
        self._last_log_ts = None  # type: ignore[var-annotated]

    def is_charge_battery_active(self) -> bool:
        """Czy manager aktywnie wymusza CHARGE_BATTERY (forced battery charging).

        Inne managery (np. WaterHeaterManager) używają jako sygnał do ochrony
        baterii przed konkurencją (np. większa rezerwacja PV).
        Zwraca False gdy mode = auto / discharge_battery (STANDBY).
        """
        return self.recommended_ems_mode == self.CHARGE_MODE

    def update(self, state: InputState) -> None:
        """Re-evaluate intervention state from current InputState.

        Called reactively by Ems on every state change.
        """
        prev_active = self.intervention_active
        prev_mode = self.recommended_ems_mode
        prev_xset = self.recommended_xset

        # 1. None-guard (core inputs)
        if self._none_present_core(state):
            self._set_neutral("none_present")
            self._log_after_update(state, prev_active, prev_mode, prev_xset)
            return

        # 2. Active window — skip pre_charge (BatteryManager rządzi)
        if self._is_in_pre_charge_window(state):
            self._set_neutral("in_pre_charge_window")
            self._log_after_update(state, prev_active, prev_mode, prev_xset)
            return

        # 3. Exit gates (gdy intervention_active)
        # Pierwsza gate: hour rollover — utility_meter resetuje balans hourly
        # o pełnej godzinie, każda godzina = osobna decyzja czy POSITIVE wystąpi.
        if self.intervention_active:
            if state.now.hour != self._intervention_started_hour:
                self._set_neutral("hour_rollover")
                self._apply_disabled_override_if_needed(state)
                self._log_after_update(state, prev_active, prev_mode, prev_xset)
                return
            exit_reason = self._exit_reason(state)
            if exit_reason is not None:
                self._set_neutral(exit_reason)
                self._apply_disabled_override_if_needed(state)
                self._log_after_update(state, prev_active, prev_mode, prev_xset)
                return
            # Continue active — re-evaluate strategy (może się zmienić)
            self._apply_strategy(state)
            self._apply_disabled_override_if_needed(state)
            self._log_after_update(state, prev_active, prev_mode, prev_xset)
            return

        # 4. Entry gates (gdy not intervention_active)
        entry_block_reason = self._entry_block_reason(state)
        if entry_block_reason is not None:
            self._set_neutral(entry_block_reason)
            self._apply_disabled_override_if_needed(state)
            self._log_after_update(state, prev_active, prev_mode, prev_xset)
            return

        # 5. Entry approved — start intervention
        self.intervention_active = True
        self._intervention_started_hour = state.now.hour
        self._apply_strategy(state)
        # 6. Final: jeśli disabled, override main outputs (intervention off),
        #    ale zachowaj would-be info w last_decision_reason
        self._apply_disabled_override_if_needed(state)
        self._log_after_update(state, prev_active, prev_mode, prev_xset)

    def _apply_disabled_override_if_needed(self, state: InputState) -> None:
        """Jeśli strategy_mode = 'disabled' (lub None) → override main outputs.

        Manager już wystawił recommended_* (would-be decision). Teraz nadpisujemy
        na AUTO/None (intervention off, listener cofa Goodwe), zachowując
        diagnostykę w last_decision_reason.

        Defensive: None → traktuj jak "disabled" (safe default gdy helper
        jeszcze nieskonfigurowany).
        """
        mode = state.grid_export_strategy_mode
        if mode == self.STRATEGY_MODE_CHARGE_ADAPTIVE:
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
        """Pick STANDBY (low PV) or charge_adaptive lookup-based Xset.

        Wywoływane tylko dla `STRATEGY_MODE_CHARGE_ADAPTIVE` (jedyna aktywna
        strategia po cleanup) — `disabled` jest filtrowany wcześniej w `update()`.
        """
        # 1. PV niskie → STANDBY (najwyższy priorytet, nawet w trakcie active).
        # Używamy avg 2min — chwilowy pv_power flapuje (~200W spike-down gdy
        # inwerter krótko "przymuli się"). Fallback do chwilowego gdy avg=None
        # (np. po restart HA, sensor jeszcze nie zebrał próbek przez 2min).
        pv_for_standby = (
            state.pv_power_avg_2_minutes
            if state.pv_power_avg_2_minutes is not None
            else state.pv_power
        )
        if pv_for_standby < self.PV_STANDBY_THRESHOLD_W:
            self.recommended_ems_mode = self.STANDBY_MODE
            self.recommended_xset = 0
            self.last_decision_reason = "low_pv_standby"
            return

        # 2. charge_adaptive — CHARGE_BATTERY z lookup-based Xset.
        # pv_available = -consumption_minus_pv_2_minutes (ujemne sensor =
        # surplus PV ponad dom-bez-heaters). Każdy bucket zwiększa Xset o 1000W
        # ponad próg pv_available — średnio 1.5 kW import z grida.
        if state.consumption_minus_pv_2_minutes is None:
            self._set_neutral("none_consumption_minus_pv_2_minutes")
            return

        # Low BMS shortcut — bateria clamp ~2 kW, nie ma sensu kombinować
        # z lookup. Stałe Xset 3500 (BMS ograniczy do BMS_max).
        if (
            state.battery_charge_limit is not None
            and state.battery_charge_limit <= self.BMS_LOW_LIMIT_A
        ):
            self.recommended_ems_mode = self.CHARGE_MODE
            self.recommended_xset = self.CHARGE_ADAPTIVE_LOW_BMS_XSET_W
            self.last_decision_reason = (
                f"charge_adaptive_low_bms_{self.CHARGE_ADAPTIVE_LOW_BMS_XSET_W}W"
            )
            return

        pv_available = -state.consumption_minus_pv_2_minutes
        # Hysteresis — jeśli current Xset jest w lookup i pv_available
        # mieści się w rozszerzonym range (±300W), zostań przy current.
        # Pierwszy tick (current_xset=None) lub poza lookup → fresh lookup.
        current_xset = self.recommended_xset
        current_range = (
            self._charge_adaptive_xset_range(current_xset)
            if current_xset is not None
            else None
        )
        if current_range is not None:
            lower, upper = current_range
            hyst = self.CHARGE_ADAPTIVE_HYSTERESIS_W
            if (lower - hyst) < pv_available <= (upper + hyst):
                self.recommended_ems_mode = self.CHARGE_MODE
                self.recommended_xset = current_xset
                self.last_decision_reason = (
                    f"charge_adaptive_stay_{current_xset}W_"
                    f"pv_avail_{int(pv_available)}"
                )
                return
        for threshold, xset in self.CHARGE_ADAPTIVE_BUCKETS:
            if pv_available > threshold:
                self.recommended_ems_mode = self.CHARGE_MODE
                self.recommended_xset = xset
                self.last_decision_reason = (
                    f"charge_adaptive_{xset}W_pv_avail_{int(pv_available)}"
                )
                return
        # pv_available ≤ -1000 → mode=AUTO ale zostań w intervention
        # (NIE _set_neutral — exit dopiero przy standardowych gates;
        # block_discharge w battery.py przejmuje gdy hourly idzie negative).
        self.recommended_ems_mode = self.AUTO_MODE
        self.recommended_xset = None
        self.last_decision_reason = f"charge_adaptive_auto_pv_avail_{int(pv_available)}"

    def _set_neutral(self, reason: str) -> None:
        """Reset to AUTO mode with given reason. Idempotent."""
        self.intervention_active = False
        self.recommended_ems_mode = self.AUTO_MODE
        self.recommended_xset = None
        self.last_decision_reason = reason
        self._intervention_started_hour = None

    @classmethod
    def _charge_adaptive_xset_range(cls, xset: int) -> tuple[float, float] | None:
        """Range pv_available który aktywowałby dany Xset z lookup table.

        Zwraca (lower, upper) lub None gdy xset nie jest w CHARGE_ADAPTIVE_BUCKETS
        (np. low_bms_shortcut 3500 — fallback do plain lookup).
        Najwyższy bucket ma upper=inf.
        """
        bucket_xsets = [x for _, x in cls.CHARGE_ADAPTIVE_BUCKETS]
        if xset not in bucket_xsets:
            return None
        idx = bucket_xsets.index(xset)
        lower = float(cls.CHARGE_ADAPTIVE_BUCKETS[idx][0])
        upper = (
            float("inf") if idx == 0 else float(cls.CHARGE_ADAPTIVE_BUCKETS[idx - 1][0])
        )
        return (lower, upper)

    # --- logging ---

    def _log_after_update(
        self,
        state: InputState,
        prev_active: bool,
        prev_mode: str,
        prev_xset: int | None,
    ) -> None:
        """INFO transition + DEBUG snapshot (throttled)."""
        if (
            prev_active != self.intervention_active
            or prev_mode != self.recommended_ems_mode
            or prev_xset != self.recommended_xset
        ):
            _LOGGER.info(
                "GridExportManager transition: active %s→%s, mode %s→%s, "
                "xset %s→%s, reason=%s",
                prev_active,
                self.intervention_active,
                prev_mode,
                self.recommended_ems_mode,
                prev_xset,
                self.recommended_xset,
                self.last_decision_reason,
            )
        self._maybe_log_snapshot(state)

    def _maybe_log_snapshot(self, state: InputState) -> None:
        """Log DEBUG snapshot gdy key fields się zmienią LUB minął throttle interval."""
        snapshot = (
            self.intervention_active,
            self.recommended_ems_mode,
            self.recommended_xset,
            self.last_decision_reason,
        )
        now = state.now
        should_log = (
            self._last_log_snapshot is None
            or snapshot != self._last_log_snapshot
            or self._last_log_ts is None
            or (
                now is not None
                and (now - self._last_log_ts).total_seconds()
                >= self.DEBUG_LOG_THROTTLE_SEC
            )
        )
        if not should_log:
            return
        cmpv = state.consumption_minus_pv_2_minutes
        pv_avail = -cmpv if cmpv is not None else None
        _LOGGER.debug(
            "GridExportManager: now=%s active=%s mode=%s xset=%s reason=%s | "
            "strategy=%s hourly=%s soc=%s pv=%s pv_avg2m=%s pv_avail=%s "
            "charge_limit=%s toggle=%s",
            now.strftime("%H:%M:%S") if now else "?",
            self.intervention_active,
            self.recommended_ems_mode,
            self.recommended_xset,
            self.last_decision_reason,
            state.grid_export_strategy_mode,
            state.exported_energy_hourly,
            state.battery_soc,
            state.pv_power,
            state.pv_power_avg_2_minutes,
            int(pv_avail) if pv_avail is not None else None,
            state.battery_charge_limit,
            state.battery_charge_toggle_on,
        )
        self._last_log_snapshot = snapshot
        self._last_log_ts = now
