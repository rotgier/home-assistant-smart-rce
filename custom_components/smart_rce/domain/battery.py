"""Battery discharge management.

Pure domain — bez HA imports, bez logowania. Persistence (Store) trzymana
w application service `BatteryStatePersistence` w `adapter.py`; logging
trzymane w application service `BatteryManagerLogger` (czyta
`diagnostic_snapshot()`).

Monitoruje bilans godzinowy i okna pre/post-charge by decydować kiedy
zablokować rozładowywanie baterii. Block_charge handling przeniesiony
do GridExportManager (Etap 2 — NEGATIVE balance).

Decyzje:

- `should_block_battery_discharge` — aktywny:
  - **Pre-charge** (7:00 → `start_charge_hour_override`): hour-start reset
    + hysteresis 100/50 na `exported_energy_hourly`. Trzyma baterię gdy
    hour netto export > 100 Wh, odblokowuje gdy < 50 Wh.
  - **Post-charge** (`start_charge_hour_override` → 13:00): continuous
    check na `consumption_minus_pv_5_minutes` (sustained trend).
    Hysteresis -500/0 W. Unika cycling baterii (charge↔discharge) typowego
    dla post-charge phase gdzie charge_current>0.

`update()` jest thin dispatcher delegujący do sub-method per phase.
Każda sub-method ustawia `self._phase` (str label dla diagnostic) ORAZ
mutuje `should_block_battery_discharge` / `_last_hour_seen` zgodnie z
logiką gałęzi. `diagnostic_snapshot(state)` czyta `_phase` field — nie
recomputuje klasyfikacji (single source of truth).

Patrz `context/target_soc_algorithm.md` dla szerszego kontekstu.
"""

from __future__ import annotations

from typing import Any, Final

from custom_components.smart_rce.domain.input_state import InputState

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

# --- discharge guard (afternoon window) --- #

# Afternoon window: 13:00 → 19:00. Po 19:00 przejmują dotychczasowe automations
# (Set SOC 90 at 19, evening discharge). Dynamic block_discharge active tylko
# gdy state.rce_should_hold_for_peak=False (low-price upcoming peaks).
AFTERNOON_WINDOW_START_HOUR: Final[int] = 13
AFTERNOON_WINDOW_END_HOUR: Final[int] = 19

# Continuous check na `pv_available_5min` (W; = -consumption_minus_pv_5_minutes):
# dodatnie = PV > cons (surplus), ujemne = cons > PV (deficit).
# Hysteresis wyzwala block_discharge gdy sustained surplus >500W,
# resetuje gdy sustained deficit <0W. Dead zone 0..500 → keep state.
PV_AVAIL_5MIN_SURPLUS_W: Final[int] = 500
PV_AVAIL_5MIN_DEFICIT_W: Final[int] = 0


class BatteryManager:
    def __init__(self) -> None:
        self.should_block_battery_discharge: bool = False
        self._last_hour_seen: int | None = None
        # _phase ustawiany przez update() (thin dispatcher → sub-method).
        # Initial "none-present" — dopóki nie ma pierwszego update z full state.
        self._phase: str = "none-present"

    def update(self, state: InputState) -> None:
        """Thin dispatcher — klasyfikuje phase, deleguje do sub-method.

        Każda sub-method jest odpowiedzialna za:
        1. Ustawienie `self._phase` (label dla diagnostic)
        2. Mutację `should_block_battery_discharge` zgodnie z logiką okna
        3. Reset `_last_hour_seen` jeśli okno tego wymaga
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
        """OVERRIDE: intencjonalne rozładowanie (np. Battery Discharge Max).

        Gdy input_boolean.ems_allow_discharge_override=True, EMS "stoi z boku".
        block_discharge wymuszone na False — pozwalamy innym automations
        swobodnie sterować baterią bez interferencji.
        """
        self._phase = "override"
        self.should_block_battery_discharge = False
        self._last_hour_seen = None

    def _update_pre_charge(self, state: InputState) -> None:
        """Pre-charge (7:00 → start_charge_hour): hour-start reset + hysteresis."""
        if state.is_workday is None:
            # Defensive: workday sensor jeszcze niezaładowany (typowo
            # 25-50ms po HA restart). Keep state — czekamy aż sensor się
            # ustabilizuje. Bez tego mógłby się zdarzyć fałszywy reset.
            self._phase = "pre-charge-keep-state"
            return
        if state.is_workday is False:
            # Weekend/święto — passthrough (RCE płaski, brak drogich godzin)
            self._phase = "pre-charge-passthrough"
            self.should_block_battery_discharge = False
            self._last_hour_seen = None
            return

        self._phase = "pre-charge"
        exported_wh = state.exported_energy_hourly * 1000  # kWh → Wh
        if self._last_hour_seen != state.now.hour:
            self._last_hour_seen = state.now.hour
            self.should_block_battery_discharge = False  # hour-start reset
        elif exported_wh >= DISCHARGE_HYSTERESIS_SET_WH:
            self.should_block_battery_discharge = True
        elif exported_wh < DISCHARGE_HYSTERESIS_RESET_WH:
            self.should_block_battery_discharge = False
        # Dead zone 50..100 — zachowuje poprzedni stan

    def _update_post_charge(self, state: InputState) -> None:
        """Post-charge (start_charge_hour → 13:00): continuous check pv_available_5min."""
        if state.is_workday is None:
            # Defensive — patrz pre-charge.
            self._phase = "post-charge-keep-state"
            return

        self._last_hour_seen = None
        if state.is_workday is False:
            # Weekend/święto — passthrough
            self._phase = "post-charge-passthrough"
            self.should_block_battery_discharge = False
            return

        self._phase = "post-charge"
        # Workday: continuous check na pv_available_5min (sustained trend).
        pv_available_5min = state.pv_available_5min
        if pv_available_5min is None:
            self.should_block_battery_discharge = False
        elif pv_available_5min > PV_AVAIL_5MIN_SURPLUS_W:
            self.should_block_battery_discharge = True
        elif pv_available_5min < PV_AVAIL_5MIN_DEFICIT_W:
            self.should_block_battery_discharge = False
        # Dead zone 0..500 — zachowuje poprzedni stan

    def _update_afternoon(self, state: InputState) -> None:
        """Afternoon (13:00 → 19:00): static (high-price) lub dynamic (low-price)."""
        if state.rce_should_hold_for_peak is None:
            # Defensive: hold sensor jeszcze niezaładowany. Bez tego guard
            # bug 14:33 — pierwszy update widzi None, wpada w dynamic
            # branch, ustawia block_discharge=True; ~22ms później sensor
            # ładuje się jako on, BatteryManager przechodzi do static i
            # ustawia False; automation reaguje na on→off i ustawia DoD=90.
            self._phase = "afternoon-keep-state"
            return

        self._last_hour_seen = None
        if state.rce_should_hold_for_peak is True:
            # High-price mode — status quo, automation Set Min SOC to 100
            # Afternoon trzyma DoD=0 do 19:00. BatteryManager nie steruje.
            self._phase = "afternoon-static"
            self.should_block_battery_discharge = False
            return

        # Low-price mode — dynamic na pv_available_5min OR exported_wh.
        # SET (hold): instant_surplus OR hourly_net_export
        # RESET (allow): instant_deficit AND NOT hourly_net_export
        # Inne kombinacje (dead zone) → keep state
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
        """Poza wszystkich okien (przed 7:00 lub po 19:00): reset."""
        self._phase = "out-of-window"
        self.should_block_battery_discharge = False
        self._last_hour_seen = None

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
        """Pure snapshot stanu — używane przez application service do persistence."""
        return {
            "block_discharge": self.should_block_battery_discharge,
            "last_hour_seen": self._last_hour_seen,
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Pure restore z dict — używane przez application service przy starcie."""
        self.should_block_battery_discharge = data.get("block_discharge", False)
        self._last_hour_seen = data.get("last_hour_seen")

    def diagnostic_snapshot(self, state: InputState) -> dict[str, Any]:
        """Log-relevant view: phase (FIELD set by update) + decision + key inputs.

        Reads `self._phase` field (set przez ostatni `update()`) — NIE
        recomputuje klasyfikacji. `state` daje dostęp do bieżących wartości
        wejściowych dla DEBUG snapshot logu.

        Używane przez `BatteryManagerLogger` w adapter.py (registered jako
        `ems.async_add_listener`).
        """
        return {
            "phase": self._phase,
            "block_discharge": self.should_block_battery_discharge,
            "last_hour_seen": self._last_hour_seen,
            # InputState fields — selektywne (te pola które są wypisywane w DEBUG)
            "now": state.now,
            "exported_energy_hourly": state.exported_energy_hourly,
            "pv_available_5min": state.pv_available_5min,
            "depth_of_discharge": state.depth_of_discharge,
            "battery_charge_toggle_on": state.battery_charge_toggle_on,
            "battery_charge_limit": state.battery_charge_limit,
            "start_charge_hour_override": state.start_charge_hour_override,
            "ems_allow_discharge_override": state.ems_allow_discharge_override,
        }
