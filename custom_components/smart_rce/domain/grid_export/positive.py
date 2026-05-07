"""PositiveIntervention — active POSITIVE intervention session.

Aktywowana przez manager gdy bilans hourly nadmiernie pozytywny (eksport >
0.06 kWh) — manager wymusza CHARGE_BATTERY by zjeść saldo (zamiast oddawać
do grida).

Strategie wewnętrzne:
- STANDBY (discharge_battery xset=0) — gdy pv_power_avg_2_minutes < 200W (noc,
  bateria target=0, house z grida — zjada saldo POSITIVE).
- charge_adaptive — Xset z lookup na `state.pv_available`. 6 bucketów (próg → Xset).
  Dolny próg pv_available > -1000 → AUTO ale stay in intervention (block_discharge
  w battery.py przejmuje gdy hourly idzie negative).

Low BMS shortcut — gdy battery_charge_limit ≤ 7A, BMS clamp na ~2 kW, więc lookup
nie ma sensu. Stałe Xset 3500W (+1.5 kW margines, BMS i tak ograniczy).

Hysteresis ±300W na granicach bucketów — eliminuje flap'owanie gdy pv_available
oscyluje na granicy.

Pre_charge window blokuje POSITIVE entry/continue (BatteryManager rządzi przez
block_discharge hysteresis). NEGATIVE działa w pre_charge (osobna strategia).

Target meter (charge_limit > 7, reserved=3500W w water_heater, battery cap 5300W
przy xset=6000) — meter = pv_avail − heaters − battery_actual. Top bucket xset=6000
rozbity na 4 podrange wg progów water_heatera (heater_budget = pv_avail - 3500,
SMALL≥1500, BIG≥3000, BOTH≥4500):

    ┌─────────────────┬──────┬─────────┬────────┬────────────┬──────────┐
    │ Active pv_avail │ xset │ battery │ środek │ heater     │ meter    │
    ├─────────────────┼──────┼─────────┼────────┼────────────┼──────────┤
    │ 8000–9000       │ 6000 │  5300   │ 8500   │ BOTH 4.5k  │ ≈ -1300  │
    │ 6500–8000       │ 6000 │  5300   │ 7250   │ BIG 3k     │ ≈ -1050  │
    │ 5000–6500       │ 6000 │  5300   │ 5750   │ SMALL 1.5k │ ≈ -1050  │
    │ 4000–5000       │ 6000 │  5300   │ 4500   │ OFF        │  ≈ -800  │
    │ 3000–4000       │ 5000 │  5000   │ 3500   │ OFF        │   -1500  │
    │ 2000–3000       │ 4000 │  4000   │ 2500   │ OFF        │   -1500  │
    │ 1000–2000       │ 3000 │  3000   │ 1500   │ OFF        │   -1500  │
    │ 0–1000          │ 2000 │  2000   │  500   │ OFF        │   -1500  │
    │ -1000–0         │ 1000 │  1000   │ -500   │ OFF        │   -1500  │
    │ ≤ -1000         │ AUTO │    0    │   —    │     —      │    —     │
    └─────────────────┴──────┴─────────┴────────┴────────────┴──────────┘

    Niższe buckety (xset≤5000): meter = środek - xset = -1500 fixed.
    Top bucket (xset=6000, battery cap 5300W): meter zależy od grzałek
    włączonych przez water_heater — średnio ≈ -1000W (import 1 kW).
    ≤ -1000: AUTO mode, block_discharge w battery.py przejmuje.

Cross-cutting checks (manager handles, NIE w try_enter / continue_or_exit):
- ems_allow_discharge_override (global block)
- balance range (manager routes by `BALANCE_GATE_KWH`)
- too_late_in_hour entry block (manager: now ≥ XX:59:40)
- other_ems_automation_active_this_hour (manager)
- hour_rollover (manager: started_hour mismatch)
- end_of_hour_cleanup exit (manager: now ≥ XX:59:50)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Final

from custom_components.smart_rce.domain.grid_export.intervention import (
    CONTINUE,
    ContinueResult,
    EntryResult,
    InterventionDirection,
)
from custom_components.smart_rce.domain.input_state import InputState

# Mode constants (Goodwe EMS)
_AUTO_MODE: Final[str] = "auto"
_STANDBY_MODE: Final[str] = "discharge_battery"
_CHARGE_MODE: Final[str] = "charge_battery"

# Entry gate — balance > 0.06 (kompromis YAML trigger 0.07 / condition 0.04).
# Public — manager używa do balance range routing.
BALANCE_GATE_KWH: Final[float] = 0.06

# Exit < 0.05 (jak YAML wait_template). Deadzone 0.05-0.06 — akceptowalna oscylacja.
EXIT_BALANCE_KWH: Final[float] = 0.05

# SoC ceilings — entry niższy niż exit żeby uniknąć flap'owania gdy bateria
# oscyluje 99↔100 (mała ilość energii do wtłoczenia, intervention nie ma sensu).
# Exit zostaje na 100 — gdy intervention już aktywne, czekamy aż faktycznie
# naładujemy do końca.
SOC_ENTRY_CEILING: Final[int] = 99
SOC_CEILING: Final[int] = 100

# Pre-charge window: 7:00 → start_charge_hour_override (BatteryManager rządzi).
PRE_CHARGE_WINDOW_START_HOUR: Final[int] = 7

# PV niskie → STANDBY (noc, brak surplus).
PV_STANDBY_THRESHOLD_W: Final[int] = 200

# battery_charge_limit ≤ 7A → low BMS shortcut.
BMS_LOW_LIMIT_A: Final[int] = 7
# Low BMS shortcut — BMS clamp na ~2 kW, lookup zbędny.
LOW_BMS_XSET_W: Final[int] = 3500

# Hysteresis dla bucket transitions
HYSTERESIS_W: Final[int] = 300

# Adaptive charge lookup — `(lower, upper, xset)` (analog NEGATIVE).
# Aktywuje się gdy `lower < pv_available <= upper` (top: upper=None=+inf).
# Bucket centrum daje meter ≈ -1500W (xset = środek + 1500).
# pv_available ≤ -1000 → AUTO (block_discharge w battery.py przejmuje).
_ADAPTIVE_BUCKETS: Final[tuple[tuple[int, int | None, int], ...]] = (
    (4000, None, 6000),  # > 4000 W → charge 6000 (top, BMS practical max)
    (3000, 4000, 5000),  # charge 5000 (meter ~-1500 w środku)
    (2000, 3000, 4000),
    (1000, 2000, 3000),
    (0, 1000, 2000),
    (-1000, 0, 1000),  # discharge zbliża się; pv_avail ≤ -1000 → AUTO
)


@dataclass
class PositiveIntervention:
    """Active POSITIVE intervention session.

    Mutable entity — `_continue` mutuje recommended_*/last_reason via
    `_commit` helper. Manager nie tworzy nowych instancji per tick.
    """

    direction: ClassVar[InterventionDirection] = InterventionDirection.POSITIVE

    started_hour: int
    recommended_mode: str = _AUTO_MODE
    recommended_xset: int | None = None
    last_reason: str = ""
    _current_xset_for_hysteresis: int | None = field(default=None, repr=False)
    """Tracks last positive xset for hysteresis lookup. None gdy STANDBY/AUTO."""

    @classmethod
    def try_enter(cls, state: InputState) -> EntryResult:
        """Try to enter POSITIVE intervention.

        Caller (GridExportManager) MUST verify global guards first:
        - balance > BALANCE_GATE_KWH (range routing)
        - no ems_allow_discharge_override
        - not too_late_in_hour
        - not other_ems_automation_active_this_hour

        Checks intervention-specific gates, then delegates to `_enter` for
        instance creation + initial _continue.
        """
        if cls._is_in_pre_charge_window(state):
            return EntryResult.blocked("in_pre_charge_window")
        if state.battery_soc >= SOC_ENTRY_CEILING:
            return EntryResult.blocked("soc_at_entry_ceiling")
        if state.battery_charge_toggle_on is False:
            return EntryResult.blocked("toggle_off")
        return cls._enter(state)

    @classmethod
    def _enter(cls, state: InputState) -> EntryResult:
        """Create blank intervention, run initial _continue, map to EntryResult.

        Initial _current_xset_for_hysteresis=None default → fresh lookup
        (no hysteresis on entry). _continue may signal exit if no real
        intervention possible (e.g. pv_available is None) — propagated
        as EntryResult.blocked.
        """
        intervention = cls(started_hour=state.now.hour)
        result = intervention._continue(state)  # noqa: SLF001 — same-class access
        if result.is_exit:
            return EntryResult.blocked(result.exit_reason)
        return EntryResult.entered(intervention)

    def continue_or_exit(self, state: InputState) -> ContinueResult:
        """Continue POSITIVE intervention. Mutates self in-place on continue.

        Caller (GridExportManager) MUST verify global guards first:
        - hour_rollover (started_hour mismatch)
        - end_of_hour_cleanup (now ≥ XX:59:50)
        - no ems_allow_discharge_override
        """
        if self._is_in_pre_charge_window(state):
            return ContinueResult.exit_with("in_pre_charge_window")
        if state.exported_energy_hourly < EXIT_BALANCE_KWH:
            return ContinueResult.exit_with("balance_recovered")
        if state.battery_soc >= SOC_CEILING:
            return ContinueResult.exit_with("soc_ceiling_exit")
        if state.battery_charge_toggle_on is False:
            return ContinueResult.exit_with("toggle_off_exit")
        return self._continue(state)

    @staticmethod
    def _is_in_pre_charge_window(state: InputState) -> bool:
        """Pre-charge: 7:00 ≤ now < start_charge_hour_override.

        Multi-caller helper (try_enter + continue_or_exit) — umieszczone po
        ostatnim caller-ze (continue_or_exit).
        """
        if state.start_charge_hour_override is None:
            return False
        if state.now.hour < PRE_CHARGE_WINDOW_START_HOUR:
            return False
        return state.now.time() < state.start_charge_hour_override

    def _continue(self, state: InputState) -> ContinueResult:
        """Compute new decision and commit to self. Returns CONTINUE or exit.

        Used by both try_enter (initial state, no hysteresis) and
        continue_or_exit (next state, hysteresis-aware via current xset).

        Decision priority:
        1. STANDBY — pv_for_standby < 200W
        2. Low BMS shortcut — battery_charge_limit ≤ 7A
        3. charge_adaptive — hysteresis → fresh lookup → AUTO fallback
        """
        # 1. PV niskie → STANDBY (chwilowy pv_power flapuje, używamy avg 2min;
        # fallback do chwilowego gdy avg=None — np. po restart HA).
        pv_for_standby = (
            state.pv_power_avg_2_minutes
            if state.pv_power_avg_2_minutes is not None
            else state.pv_power
        )
        if pv_for_standby < PV_STANDBY_THRESHOLD_W:
            return self._commit(_STANDBY_MODE, 0, "low_pv_standby", None)

        # 2. Low BMS shortcut — bateria clamp ~2 kW, lookup zbędny.
        # NIE wymaga pv_available, więc shortcut przed guard'em.
        if (
            state.battery_charge_limit is not None
            and state.battery_charge_limit <= BMS_LOW_LIMIT_A
        ):
            return self._commit(
                _CHARGE_MODE,
                LOW_BMS_XSET_W,
                f"charge_adaptive_low_bms_{LOW_BMS_XSET_W}W",
                None,
            )

        # 3. charge_adaptive lookup — wymaga pv_available.
        if state.pv_available is None:
            return ContinueResult.exit_with("none_pv_available")
        return self._resolve_charge_adaptive(state.pv_available)

    def _resolve_charge_adaptive(self, pv_available: float) -> ContinueResult:
        """Hysteresis stay → fresh lookup → AUTO fallback (pv_avail ≤ -1000).

        Initial state z try_enter: _current_xset_for_hysteresis=None → range=None
        → hysteresis skip → fresh lookup. Continue: hysteresis stabilizuje
        oscylacje na granicach bucketów.
        """
        # Hysteresis — current Xset stable jeśli pv_available w rozszerzonym range.
        current_range = self._xset_range(self._current_xset_for_hysteresis)
        if current_range is not None:
            lower, upper = current_range
            if (lower - HYSTERESIS_W) < pv_available <= (upper + HYSTERESIS_W):
                current = self._current_xset_for_hysteresis
                return self._commit(
                    _CHARGE_MODE,
                    current,
                    f"charge_adaptive_stay_{current}W_pv_avail_{int(pv_available)}",
                    current,
                )

        # Fresh lookup
        xset = self._lookup_xset(pv_available)
        if xset is not None:
            return self._commit(
                _CHARGE_MODE,
                xset,
                f"charge_adaptive_{xset}W_pv_avail_{int(pv_available)}",
                xset,
            )

        # pv_available ≤ -1000 → mode=AUTO ale stay in intervention (NIE exit).
        # block_discharge w battery.py przejmuje gdy hourly idzie negative.
        return self._commit(
            _AUTO_MODE,
            None,
            f"charge_adaptive_auto_pv_avail_{int(pv_available)}",
            None,
        )

    @staticmethod
    def _xset_range(xset: int | None) -> tuple[float, float] | None:
        """Range pv_available który aktywowałby dany Xset.

        Zwraca (lower, upper) lub None gdy xset nie jest w _ADAPTIVE_BUCKETS
        (np. low_bms_shortcut 3500 — fallback do plain lookup).
        Najwyższy bucket ma upper=inf.
        """
        if xset is None:
            return None
        for lower, upper, xs in _ADAPTIVE_BUCKETS:
            if xs == xset:
                upper_f = float("inf") if upper is None else float(upper)
                return (float(lower), upper_f)
        return None

    @staticmethod
    def _lookup_xset(pv_available: float) -> int | None:
        """Znajdź Xset dla pv_available z _ADAPTIVE_BUCKETS.

        Returns None gdy pv_available ≤ -1000 (poza najniższym bucket'em) —
        caller przełącza na AUTO mode (stay in intervention).
        """
        for lower, upper, xset in _ADAPTIVE_BUCKETS:
            if upper is None:
                if pv_available > lower:
                    return xset
            elif lower < pv_available <= upper:
                return xset
        return None

    def _commit(
        self,
        mode: str,
        xset: int | None,
        reason: str,
        current_xset_for_hysteresis: int | None,
    ) -> ContinueResult:
        """Mutate self with new decision and return CONTINUE.

        Multi-caller helper (_continue + _resolve_charge_adaptive) — umieszczone
        po ostatnim caller-ze (_resolve_charge_adaptive) plus jego sub-helpers.
        """
        self.recommended_mode = mode
        self.recommended_xset = xset
        self.last_reason = reason
        self._current_xset_for_hysteresis = current_xset_for_hysteresis
        return CONTINUE
