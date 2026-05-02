"""Battery discharge management.

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

Patrz `context/target_soc_algorithm.md` dla szerszego kontekstu.
"""

from __future__ import annotations

import logging
from typing import Final

from custom_components.smart_rce.domain.input_state import InputState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

# --- persistence --- #

STORAGE_VERSION: Final[int] = 1
STORAGE_KEY: Final[str] = "smart_rce_battery_manager"

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

# Continuous check na `consumption_minus_pv_5_minutes` (W):
# ujemne = PV > cons (surplus), dodatnie = cons > PV (deficit).
# Hysteresis wyzwala block_discharge gdy sustained surplus >=500W,
# resetuje gdy sustained deficit >0W. Dead zone -500..0 → keep state.
AVG_5MIN_SURPLUS_THRESHOLD_W: Final[int] = -500
AVG_5MIN_DEFICIT_THRESHOLD_W: Final[int] = 0

# --- logging --- #

# Minimum interwał w sekundach między kolejnymi pełnymi DEBUG snapshotami,
# gdy stan key fields (phase + 3 flagi) się NIE zmienia. Zapobiega spamowaniu
# logów co tick gdy nic się nie dzieje.
DEBUG_LOG_THROTTLE_SEC: Final[int] = 60


class BatteryManager:
    def __init__(self, hass: HomeAssistant | None = None) -> None:
        self.should_block_battery_discharge: bool = False
        self._last_hour_seen: int | None = None
        # Throttling dla DEBUG snapshotów
        self._last_log_snapshot: tuple | None = None
        self._last_log_ts = None  # type: ignore[var-annotated]
        # Persistence — przeżywa HA restart, chroni przed race condition gdy
        # template binary_sensor (np. rce_should_hold_for_peak) ładuje się
        # 25-50ms po smart_rce. Bez restore pierwszy update widzi None i
        # mógłby mylnie ustawić block_discharge.
        self._hass = hass
        self._store: Store | None = (
            Store(hass, STORAGE_VERSION, STORAGE_KEY) if hass else None
        )

    def update(self, state: InputState) -> None:  # noqa: C901
        if self._none_present(state):
            _LOGGER.debug(
                "BatteryManager.update skipped (none_present): exported=%s now=%s",
                state.exported_energy_hourly,
                state.now,
            )
            return

        exported_energy_wh = state.exported_energy_hourly * 1000  # kWh → Wh

        # Snapshot poprzednich wartości dla detekcji transitions w logach
        # i decydowania czy persistować stan na disk.
        prev_block_discharge = self.should_block_battery_discharge
        prev_last_hour_seen = self._last_hour_seen

        # --- OVERRIDE: intencjonalne rozładowanie (np. Battery Discharge Max) ---
        # Gdy input_boolean.ems_allow_discharge_override=True, EMS "stoi z boku".
        # block_discharge wymuszone na False — pozwalamy innym automations
        # swobodnie sterować baterią bez interferencji.
        if state.ems_allow_discharge_override is True:
            self.should_block_battery_discharge = False
            self._last_hour_seen = None
            self._log_transitions(prev_block_discharge, reason="override_active")
            self._maybe_log_snapshot(state, exported_energy_wh, phase="override")
            self._maybe_save(prev_block_discharge, prev_last_hour_seen)
            return

        # --- block_discharge ---
        if self._is_in_pre_charge_window(state):
            if state.is_workday is None:
                # Defensive: workday sensor jeszcze niezaładowany (typowo
                # 25-50ms po HA restart). Keep state — czekamy aż sensor się
                # ustabilizuje. Bez tego mógłby się zdarzyć fałszywy reset.
                self._maybe_log_snapshot(
                    state, exported_energy_wh, phase="pre-charge-keep-state"
                )
                return
            if state.is_workday is False:
                # Weekend/święto — passthrough (RCE płaski, brak drogich godzin)
                self.should_block_battery_discharge = False
                self._last_hour_seen = None
            elif self._last_hour_seen != state.now.hour:
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
            if state.is_workday is None:
                # Defensive — patrz pre-charge wyżej.
                self._maybe_log_snapshot(
                    state, exported_energy_wh, phase="post-charge-keep-state"
                )
                return
            self._last_hour_seen = None
            if state.is_workday is False:
                # Weekend/święto — passthrough
                self.should_block_battery_discharge = False
            else:
                # Workday: continuous check na avg_5min (sustained trend).
                avg_5min = state.consumption_minus_pv_5_minutes
                if avg_5min is None:
                    self.should_block_battery_discharge = False
                elif avg_5min < AVG_5MIN_SURPLUS_THRESHOLD_W:
                    self.should_block_battery_discharge = True
                elif avg_5min > AVG_5MIN_DEFICIT_THRESHOLD_W:
                    self.should_block_battery_discharge = False
                # Dead zone -500..0 — zachowuje poprzedni stan
        elif self._is_in_afternoon_window(state):
            if state.rce_should_hold_for_peak is None:
                # Defensive: hold sensor jeszcze niezaładowany. Bez tego guard
                # bug 14:33 — pierwszy update widzi None, wpada w dynamic
                # branch, ustawia block_discharge=True; ~22ms później sensor
                # ładuje się jako on, BatteryManager przechodzi do static i
                # ustawia False; automation reaguje na on→off i ustawia DoD=90.
                self._maybe_log_snapshot(
                    state, exported_energy_wh, phase="afternoon-keep-state"
                )
                return
            self._last_hour_seen = None
            if state.rce_should_hold_for_peak is True:
                # High-price mode — status quo, automation Set Min SOC to 100
                # Afternoon trzyma DoD=0 do 19:00. BatteryManager nie steruje.
                self.should_block_battery_discharge = False
            else:
                # Low-price mode — dynamic na avg_5min OR exported_wh.
                # SET (hold): instant_surplus OR hourly_net_export
                # RESET (allow): instant_deficit AND NOT hourly_net_export
                # Inne kombinacje (dead zone) → keep state
                avg_5min = state.consumption_minus_pv_5_minutes
                if avg_5min is None:
                    self.should_block_battery_discharge = False
                else:
                    instant_surplus = avg_5min < AVG_5MIN_SURPLUS_THRESHOLD_W
                    instant_deficit = avg_5min > AVG_5MIN_DEFICIT_THRESHOLD_W
                    hourly_net_export = exported_energy_wh > 0
                    if instant_surplus or hourly_net_export:
                        self.should_block_battery_discharge = True
                    elif instant_deficit and not hourly_net_export:
                        self.should_block_battery_discharge = False
                    # else: keep state
        else:
            # Poza wszystkich okien: reset.
            self.should_block_battery_discharge = False
            self._last_hour_seen = None

        # --- Debug snapshot (throttled) + INFO transitions ---
        if self._is_in_pre_charge_window(state):
            phase = (
                "pre-charge-passthrough" if state.is_workday is False else "pre-charge"
            )
        elif self._is_in_post_charge_window(state):
            phase = (
                "post-charge-passthrough"
                if state.is_workday is False
                else "post-charge"
            )
        elif self._is_in_afternoon_window(state):
            phase = (
                "afternoon-static"
                if state.rce_should_hold_for_peak is True
                else "afternoon-dynamic"
            )
        else:
            phase = "out-of-window"
        self._log_transitions(prev_block_discharge, reason=phase)
        self._maybe_log_snapshot(state, exported_energy_wh, phase=phase)
        self._maybe_save(prev_block_discharge, prev_last_hour_seen)

    async def async_restore(self) -> None:
        """Wywołać RAZ przed pierwszym update() w async_setup_entry."""
        if not self._store:
            return
        data = await self._store.async_load()
        if not data:
            return
        self.should_block_battery_discharge = data.get("block_discharge", False)
        self._last_hour_seen = data.get("last_hour_seen")
        _LOGGER.info(
            "BatteryManager restored: block_discharge=%s last_hour=%s",
            self.should_block_battery_discharge,
            self._last_hour_seen,
        )

    def _maybe_save(
        self,
        prev_block_discharge: bool,
        prev_last_hour_seen: int | None,
    ) -> None:
        """Persist state on every change (no throttle — flapping rzadkie, write tani)."""
        if not self._store or not self._hass:
            return
        if (
            prev_block_discharge == self.should_block_battery_discharge
            and prev_last_hour_seen == self._last_hour_seen
        ):
            return
        self._hass.async_create_task(self._async_save())

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "block_discharge": self.should_block_battery_discharge,
                "last_hour_seen": self._last_hour_seen,
            }
        )

    def _maybe_log_snapshot(
        self, state: InputState, exported_energy_wh: float, *, phase: str
    ) -> None:
        """Log DEBUG snapshot gdy key fields się zmienią LUB minął throttle interval.

        Zapobiega spamowaniu logów co tick gdy nic się nie zmienia
        (bateria stoi w stable state przez minuty).
        """
        # Key fields których zmiana jest warta logowania
        snapshot = (
            phase,
            self.should_block_battery_discharge,
        )

        now = state.now
        should_log = (
            self._last_log_snapshot is None
            or snapshot != self._last_log_snapshot
            or self._last_log_ts is None
            or (now - self._last_log_ts).total_seconds() >= DEBUG_LOG_THROTTLE_SEC
        )
        if not should_log:
            return

        _LOGGER.debug(
            "BatteryManager[%s] now=%s exported=%+.3fkWh(%+dWh) avg_5min=%s "
            "DoD=%s toggle=%s charge_limit=%s override_window=%s | "
            "block_discharge=%s",
            phase,
            now.strftime("%H:%M:%S") if now else "?",
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
        )
        self._last_log_snapshot = snapshot
        self._last_log_ts = now

    def _log_transitions(
        self,
        prev_block_discharge: bool,
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
