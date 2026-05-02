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
- POSITIVE charge_adaptive           — Xset z lookup na `state.pv_available`
                                       (PV − dom_bez_heaters); pv_avail ≤ -1000 → AUTO
- NEGATIVE adaptive                  — charge/discharge buckets na pv_available;
                                       target meter +1500W eksport

Decision tree (`grid_export_strategy_mode`):
- "charge_adaptive" → domyślne aktywne (POSITIVE i NEGATIVE)
- "disabled"        → manager evaluuje, ale intervention off (diagnostic only)

Hysteresis: w lookup — current Xset stable jeśli pv_available mieści się
w rozszerzonym range (±300W od bucket boundaries). Eliminuje flap'owanie
gdy pv_available oscyluje na granicy.

Defensive: gdy `state.pv_available` lub `battery_charge_limit` są None
(np. po HA restart, sensory unavailable przez ~25-50ms) → no-op, manager
wraca do AUTO, listener wraca rejestry do AUTO.
"""

from __future__ import annotations

from enum import StrEnum
import logging
from typing import Final

from custom_components.smart_rce.domain.input_state import InputState

_LOGGER = logging.getLogger(__name__)


class InterventionDirection(StrEnum):
    """Kierunek aktywnej interwencji GridExportManager.

    POSITIVE — bilans hourly nadmiernie pozytywny (eksport > 0.06 kWh),
    manager wymusza CHARGE_BATTERY (lub STANDBY przy niskim PV) by zjeść saldo.

    NEGATIVE — bilans hourly negatywny (import netto), manager wymusza
    adaptive charge/discharge by ustabilizować meter ≈ +1500W eksport.
    """

    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"


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

    # NEGATIVE balance gates (time-dependent entry threshold).
    # Pre-45min: entry gdy hourly < -0.05 (toleruj umiarkowane negative,
    # czas na natural recovery z PV).
    # Post-45min: entry gdy hourly < 0 (każdy negative — godzina się kończy).
    # Exit zawsze gdy hourly > 0 (saldo recovery).
    NEGATIVE_ENTRY_THRESHOLD_EARLY_KWH: Final[float] = -0.05
    NEGATIVE_ENTRY_THRESHOLD_LATE_KWH: Final[float] = 0.0
    NEGATIVE_EXIT_BALANCE_KWH: Final[float] = 0.0
    NEGATIVE_LATE_HALF_HOUR_MINUTE: Final[int] = 45
    NEGATIVE_SOC_HARD_FLOOR: Final[int] = 10

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
    # Active discharge (Xset > 0). Wartość ta sama co STANDBY_MODE — semantyka
    # inna (NEGATIVE active discharge zamiast POSITIVE bateria stop).
    DISCHARGE_MODE: Final[str] = "discharge_battery"
    CHARGE_MODE: Final[str] = "charge_battery"

    # Strategy modes (input_select.smart_rce_grid_export_strategy_mode)
    STRATEGY_MODE_DISABLED: Final[str] = "disabled"
    STRATEGY_MODE_CHARGE_ADAPTIVE: Final[str] = "charge_adaptive"

    # Adaptive charge lookup — `state.pv_available` mapped na Xset.
    # Każdy próg w (W). Pierwszy spełniający → Xset.
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

    # NEGATIVE adaptive buckets (target meter +1500W eksport).
    # Format: (lower, upper, xset_signed) — bucket aktywuje się gdy
    # `lower < pv_avail <= upper` (dla najwyższego upper=None oznacza +inf).
    # - xset_signed > 0 → charge_battery z xset = xset_signed
    # - xset_signed = 0 → discharge_battery z xset = 0 (bucket STOP, bateria stoi)
    # - xset_signed < 0 → discharge_battery z xset = abs(xset_signed)
    # Bucket centrum daje ekport ~1500W (Xset = lower - 1000).
    NEGATIVE_ADAPTIVE_BUCKETS: Final[tuple[tuple[int, int | None, int], ...]] = (
        (5000, None, 4000),  # > 5000 W → charge 4000 (eksport ≥ 1000)
        (4000, 5000, 3000),  # charge 3000 (eksport ~1500)
        (3000, 4000, 2000),
        (2000, 3000, 1000),
        (1000, 2000, 0),  # bucket STOP — bateria stoi, eksport = pv_avail
        (0, 1000, -1000),  # discharge 1000 (eksport ~1500)
        (-1000, 0, -2000),
        (-2000, -1000, -3000),
        (-3000, -2000, -4000),
        (-4000, -3000, -6000),  # discharge 6000 (BMS max ~5.2-5.3 kW)
        # pv_avail ≤ -4000: fallback do cap (-6000), brak osobnego bucketu
    )

    # Logowanie — throttle DEBUG snapshot gdy nic się nie zmienia (60s).
    DEBUG_LOG_THROTTLE_SEC: Final[int] = 60

    def __init__(self) -> None:
        self.intervention_active: bool = False
        self.intervention_direction: InterventionDirection | None = None
        self.recommended_ems_mode: str = self.AUTO_MODE
        self.recommended_xset: int | None = None
        self.last_decision_reason: str | None = None
        self._intervention_started_hour: int | None = None
        # Throttling dla DEBUG snapshotów (jak w BatteryManager)
        self._last_log_snapshot: tuple | None = None
        self._last_log_ts = None  # type: ignore[var-annotated]

    def get_active_intervention(self) -> InterventionDirection | None:
        """Aktualny kierunek interwencji (POSITIVE/NEGATIVE/None).

        Używane przez WaterHeaterManager do dyfferencjacji reserved (większy
        reserved przy NEGATIVE — grzałki off priorytetowo).
        """
        if not self.intervention_active:
            return None
        return self.intervention_direction

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

        # 2. Exit gates (gdy intervention_active) — branch po direction.
        # Hour rollover wspólny: utility_meter resetuje balans hourly o pełnej
        # godzinie, każda godzina = osobna decyzja czy intervention.
        # Pre-charge window obsługiwany przez `_positive_*` gates (POSITIVE
        # blocked, NEGATIVE działa — jeśli SoC > min_soc).
        if self.intervention_active:
            if state.now.hour != self._intervention_started_hour:
                self._set_neutral("hour_rollover")
                self._apply_disabled_override_if_needed(state)
                self._log_after_update(state, prev_active, prev_mode, prev_xset)
                return
            if self.intervention_direction is InterventionDirection.NEGATIVE:
                self._continue_negative(state)
            else:
                self._continue_positive(state)
            self._apply_disabled_override_if_needed(state)
            self._log_after_update(state, prev_active, prev_mode, prev_xset)
            return

        # 3. Entry gates — POSITIVE i NEGATIVE są mutually exclusive (różne
        # progi hourly), kolejność sprawdzania arbitralna.
        pos_block = self._positive_entry_block_reason(state)
        if pos_block is None:
            self.intervention_active = True
            self.intervention_direction = InterventionDirection.POSITIVE
            self._intervention_started_hour = state.now.hour
            self._apply_strategy_positive(state)
            self._apply_disabled_override_if_needed(state)
            self._log_after_update(state, prev_active, prev_mode, prev_xset)
            return

        neg_block = self._negative_entry_block_reason(state)
        if neg_block is None:
            self.intervention_active = True
            self.intervention_direction = InterventionDirection.NEGATIVE
            self._intervention_started_hour = state.now.hour
            self._apply_negative_with_clamp(state)
            self._apply_disabled_override_if_needed(state)
            self._log_after_update(state, prev_active, prev_mode, prev_xset)
            return

        # Neither direction — neutral z join'em obu block reasons (oba są
        # not None bo wcześniejsze if'y already returned przy entry approval).
        # Pokazuje DLACZEGO żaden kierunek nie wszedł w intervention.
        self._set_neutral(f"pos:{pos_block} | neg:{neg_block}")
        self._apply_disabled_override_if_needed(state)
        self._log_after_update(state, prev_active, prev_mode, prev_xset)

    def _continue_positive(self, state: InputState) -> None:
        """Continue POSITIVE intervention — exit check + apply."""
        exit_reason = self._positive_exit_reason(state)
        if exit_reason is not None:
            self._set_neutral(exit_reason)
            return
        self._apply_strategy_positive(state)

    def _continue_negative(self, state: InputState) -> None:
        """Continue NEGATIVE intervention — pre-compute xset + clamp + exit."""
        if state.pv_available is None:
            self._set_neutral("none_pv_available")
            return
        pv_available = state.pv_available
        xset_signed, is_stay = self._negative_resolve_xset_with_hysteresis(pv_available)
        xset_signed, is_stay = self._negative_clamp_charge_bucket(
            xset_signed, is_stay, state
        )
        # Exit check (z aktualnym xset_signed po clamp)
        exit_reason = self._negative_exit_reason(state, xset_signed)
        if exit_reason is not None:
            self._set_neutral(exit_reason)
            return
        prefix = "negative_stay" if is_stay else "negative"
        self._apply_signed_xset(xset_signed, prefix, pv_available)

    def _apply_negative_with_clamp(self, state: InputState) -> None:
        """Entry NEGATIVE — compute xset, clamp, apply.

        Defensive: caller (`update()`) wywołuje tylko gdy entry gates passed
        (m.in. `pv_available is not None`), ale dodajemy explicit guard
        dla type-safety + odporności na refactor.
        """
        if state.pv_available is None:
            self._set_neutral("none_pv_available")
            return
        pv_available = state.pv_available
        xset_signed = self._negative_lookup(pv_available)
        xset_signed, _ = self._negative_clamp_charge_bucket(xset_signed, False, state)
        self._apply_signed_xset(xset_signed, "negative", pv_available)

    def _negative_clamp_charge_bucket(
        self, xset_signed: int, is_stay: bool, state: InputState
    ) -> tuple[int, bool]:
        """Clamp charge bucket (xset>0) do bucket STOP gdy bateria pełna lub toggle off.

        - SoC = 100 → bateria pełna, nie ma jak ładować, ale eksport z PV
          niweluje NEGATIVE (bucket STOP daje pv_avail eksport).
        - battery_charge_toggle_on = False → user wyłączył ładowanie, manager
          szanuje (bucket STOP). NEGATIVE branch nadal aktywny — pv_avail
          eksport ratuje saldo.
        """
        if xset_signed <= 0:
            return xset_signed, is_stay
        if (
            state.battery_soc is not None
            and state.battery_soc >= self.POSITIVE_SOC_CEILING
        ):
            return 0, False
        if state.battery_charge_toggle_on is False:
            return 0, False
        return xset_signed, is_stay

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
        self.intervention_direction = None
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
    def _positive_exit_reason(cls, state: InputState) -> str | None:
        """Return exit reason if any exit gate fires, else None."""
        if cls._is_in_pre_charge_window(state):
            return "in_pre_charge_window"
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
    def _positive_entry_block_reason(cls, state: InputState) -> str | None:
        """Return reason if entry blocked, else None (entry allowed).

        Pre-charge window blocks POSITIVE — BatteryManager rządzi (block_discharge
        hysteresis). NEGATIVE może działać w pre_charge (osobny gate).
        """
        if cls._is_in_pre_charge_window(state):
            return "in_pre_charge_window"
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

    # --- NEGATIVE gates ---

    @classmethod
    def _negative_entry_threshold(cls, state: InputState) -> float:
        """Time-dependent entry threshold dla NEGATIVE.

        Pre-45min: -0.05 (toleruj umiarkowane negative, czas na natural recovery).
        Post-45min: 0.0 (każdy negative — godzina się kończy).
        """
        if state.now.minute < cls.NEGATIVE_LATE_HALF_HOUR_MINUTE:
            return cls.NEGATIVE_ENTRY_THRESHOLD_EARLY_KWH
        return cls.NEGATIVE_ENTRY_THRESHOLD_LATE_KWH

    @classmethod
    def _negative_entry_block_reason(cls, state: InputState) -> str | None:
        """Return reason if entry blocked, else None (entry allowed).

        Filozofia: entry tylko gdy bucket może coś realnie zrobić.
        - bucket DISCHARGE wymaga SoC > min_soc (energia do oddania)
        - bucket CHARGE + SoC=100 → entry pozwolony (clamp do bucket STOP)
        - bucket STOP zawsze feasible

        EMS override (`ems_allow_discharge_override=True`) blokuje NEGATIVE —
        user wymusza discharge (np. Battery Discharge Max), nie ingerujemy.
        POSITIVE może nadal działać — force charge nie konfliktuje z user
        intent (override dotyczy discharge).
        """
        if state.ems_allow_discharge_override is True:
            return "ems_allow_discharge_override"
        threshold = cls._negative_entry_threshold(state)
        if state.exported_energy_hourly >= threshold:
            return f"balance_above_neg_threshold_{threshold:.2f}"
        if state.battery_soc <= cls.NEGATIVE_SOC_HARD_FLOOR:
            return "soc_below_hard_floor"
        if state.depth_of_discharge is None:
            return "none_depth_of_discharge"
        if state.pv_available is None:
            return "none_pv_available"
        if not (
            state.now.minute < cls.LATE_HOUR_MINUTE
            or state.now.second < cls.LATE_HOUR_SECOND
        ):
            return "too_late_in_hour"
        if state.other_ems_automation_active_this_hour is True:
            return "other_automation_active"
        # Feasibility — bucket discharge wymaga SoC > min_soc.
        # Bucket charge przy SoC=100 NIE blokuje (clamp do bucket STOP).
        pv_available = state.pv_available
        xset_signed = cls._negative_lookup_static(pv_available)
        if xset_signed < 0 and state.battery_soc <= (100 - state.depth_of_discharge):
            return "soc_at_dod_floor_no_discharge"
        return None

    @classmethod
    def _negative_exit_reason(
        cls, state: InputState, current_xset_signed: int
    ) -> str | None:
        """Exit gdy bucket discharge przestał być feasible lub override aktywne.

        `current_xset_signed` = xset PO clamp w `_continue_negative`.
        Bucket charge + SoC=100 jest już clamp'owany do 0, więc tutaj
        widzimy tylko discharge (xset_signed<0) lub stop (xset_signed=0).
        """
        if state.ems_allow_discharge_override is True:
            return "ems_allow_discharge_override"
        if state.exported_energy_hourly > cls.NEGATIVE_EXIT_BALANCE_KWH:
            return "negative_balance_recovered"
        if state.depth_of_discharge is None:
            return "none_depth_of_discharge_exit"
        if current_xset_signed < 0 and state.battery_soc <= (
            100 - state.depth_of_discharge
        ):
            return "soc_at_dod_floor_exit"
        if (
            state.now.minute >= cls.EXIT_END_OF_HOUR_MINUTE
            and state.now.second >= cls.EXIT_END_OF_HOUR_SECOND
        ):
            return "end_of_hour_cleanup"
        return None

    # --- NEGATIVE adaptive lookup ---

    @classmethod
    def _negative_lookup_static(cls, pv_available: float) -> int:
        """Znajdź xset_signed dla pv_available z NEGATIVE_ADAPTIVE_BUCKETS."""
        for lower, upper, xset_signed in cls.NEGATIVE_ADAPTIVE_BUCKETS:
            if upper is None:
                if pv_available > lower:
                    return xset_signed
            elif lower < pv_available <= upper:
                return xset_signed
        # Fallback: cap przy najgłębszym bucket (pv_avail ≤ -4000) → -6000
        return cls.NEGATIVE_ADAPTIVE_BUCKETS[-1][2]

    def _negative_lookup(self, pv_available: float) -> int:
        return self._negative_lookup_static(pv_available)

    @classmethod
    def _negative_adaptive_xset_range(
        cls, xset_signed: int | None
    ) -> tuple[float, float] | None:
        """Range pv_available który aktywowałby dany xset_signed.

        Zwraca (lower, upper) lub None gdy xset_signed nie jest w bucketach.
        Najwyższy bucket ma upper=inf.
        """
        if xset_signed is None:
            return None
        for lower, upper, xs in cls.NEGATIVE_ADAPTIVE_BUCKETS:
            if xs == xset_signed:
                upper_f = float("inf") if upper is None else float(upper)
                return (float(lower), upper_f)
        return None

    def _negative_resolve_xset_with_hysteresis(
        self, pv_available: float
    ) -> tuple[int, bool]:
        """Lookup xset_signed z hysteresis (current bucket + ±300W tolerance).

        Zwraca (xset_signed, is_stay):
        - is_stay=True gdy hysteresis utrzymuje current bucket (pv_avail
          w rozszerzonym range), is_stay=False gdy fresh lookup (zmiana bucketu).
        """
        current_xset_signed = self._signed_xset()
        current_range = self._negative_adaptive_xset_range(current_xset_signed)
        if current_range is not None:
            lower, upper = current_range
            hyst = self.CHARGE_ADAPTIVE_HYSTERESIS_W
            if (lower - hyst) < pv_available <= (upper + hyst):
                return current_xset_signed, True  # type: ignore[return-value]
        return self._negative_lookup(pv_available), False

    def _signed_xset(self) -> int | None:
        """Aktualny xset_signed z (mode, xset). None gdy auto/idle."""
        if self.recommended_xset is None:
            return None
        if self.recommended_ems_mode == self.CHARGE_MODE:
            return self.recommended_xset
        if self.recommended_ems_mode in (self.STANDBY_MODE, self.DISCHARGE_MODE):
            # xset=0 → bucket stop; xset>0 → bucket discharge → -xset
            return -self.recommended_xset if self.recommended_xset > 0 else 0
        return None

    def _apply_signed_xset(
        self, xset_signed: int, prefix: str, pv_available: float
    ) -> None:
        """Apply mode/xset z xset_signed (NEGATIVE strategy output).

        - xset_signed > 0 → charge_battery z xset = xset_signed
        - xset_signed = 0 → discharge_battery z xset = 0 (bucket STOP)
        - xset_signed < 0 → discharge_battery z xset = abs(xset_signed)
        """
        if xset_signed > 0:
            self.recommended_ems_mode = self.CHARGE_MODE
            self.recommended_xset = xset_signed
            self.last_decision_reason = (
                f"{prefix}_charge_{xset_signed}W_pv_avail_{int(pv_available)}"
            )
        elif xset_signed == 0:
            self.recommended_ems_mode = self.STANDBY_MODE
            self.recommended_xset = 0
            self.last_decision_reason = (
                f"{prefix}_stop_xset_0_pv_avail_{int(pv_available)}"
            )
        else:
            self.recommended_ems_mode = self.DISCHARGE_MODE
            self.recommended_xset = abs(xset_signed)
            self.last_decision_reason = (
                f"{prefix}_discharge_{abs(xset_signed)}W_"
                f"pv_avail_{int(pv_available)}"
            )

    # --- strategy ---

    def _apply_strategy_positive(self, state: InputState) -> None:
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

        # Low BMS shortcut — bateria clamp ~2 kW, nie ma sensu kombinować
        # z lookup. Stałe Xset 3500 (BMS ograniczy do BMS_max).
        # NIE wymaga pv_available, więc shortcut przed guard'em.
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

        # Lookup-based Xset wymaga pv_available (surplus PV ponad dom-bez-heaters).
        # Każdy bucket zwiększa Xset o 1000W ponad próg pv_available — średnio
        # 1.5 kW import z grida.
        if state.pv_available is None:
            self._set_neutral("none_pv_available")
            return
        pv_available = state.pv_available
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
        self.intervention_direction = None
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
        pv_avail = state.pv_available
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
