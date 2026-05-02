"""POSITIVE strategy — force charge_adaptive / STANDBY gdy hourly eksport > threshold.

Aktywowana gdy bilans hourly nadmiernie pozytywny (eksport > 0.06 kWh) — manager
wymusza CHARGE_BATTERY by zjeść saldo (zamiast oddawać do grida).

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

Pre_charge window blokuje POSITIVE (BatteryManager rządzi przez block_discharge
hysteresis). NEGATIVE działa w pre_charge (osobna strategia, osobna decyzja).

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

Wniosek: meter w pełnym zakresie pv_avail oscyluje ≈ -1000..-1500W (import).
POSITIVE intervention działa praktycznie zawsze (instalacja PV 9.07 kWp,
dom konsumuje >0 → pv_avail nigdy nie przekracza ~9 kW).

Edge cases (świadomie nie obsłużone):
- BMS clamp przy SoC>90% (battery realnie 2-3 kW): xset=6000 to "wysokie życzenie",
  BMS wins. Grzałki próbują nadgonić (3-4.5 kW dodatkowego load).
"""

from __future__ import annotations

from typing import Final

from custom_components.smart_rce.domain.input_state import InputState


class PositiveStrategy:
    """Strategy dla POSITIVE balance — entry gates, exit gates, charge_adaptive."""

    # Entry > 0.06 (kompromis YAML trigger 0.07 / condition 0.04).
    # Exit < 0.05 (jak YAML wait_template).
    # Deadzone 0.05-0.06 — akceptowalna oscylacja.
    BALANCE_GATE_KWH: Final[float] = 0.06
    EXIT_BALANCE_KWH: Final[float] = 0.05
    SOC_CEILING: Final[int] = 100
    # Entry threshold niższy niż exit żeby uniknąć flap'owania gdy bateria
    # oscyluje 99↔100 (bardzo mała ilość energii do wtłoczenia, intervention
    # nie ma sensu). Exit zostaje na 100 — gdy intervention już aktywne,
    # czekamy aż faktycznie naładujemy do końca.
    SOC_ENTRY_CEILING: Final[int] = 99

    LATE_HOUR_MINUTE: Final[int] = 59
    LATE_HOUR_SECOND: Final[int] = 40
    EXIT_END_OF_HOUR_MINUTE: Final[int] = 59
    EXIT_END_OF_HOUR_SECOND: Final[int] = 50

    # Pre-charge window: 7:00 → start_charge_hour_override (BatteryManager rządzi).
    PRE_CHARGE_WINDOW_START_HOUR: Final[int] = 7

    # PV niskie → STANDBY (noc, brak surplus).
    PV_STANDBY_THRESHOLD_W: Final[int] = 200
    # battery_charge_limit ≤ 7A → low BMS shortcut.
    BMS_LOW_LIMIT_A: Final[int] = 7

    HYSTERESIS_W: Final[int] = 300

    # Mode constants
    AUTO_MODE: Final[str] = "auto"
    STANDBY_MODE: Final[str] = "discharge_battery"
    CHARGE_MODE: Final[str] = "charge_battery"

    # Adaptive charge lookup — `(lower, upper, xset)` (analog NEGATIVE).
    # Aktywuje się gdy `lower < pv_available <= upper` (top: upper=None=+inf).
    # Bucket centrum daje meter ≈ -1500W (xset = środek + 1500).
    # pv_available ≤ -1000 → AUTO (block_discharge w battery.py przejmuje).
    ADAPTIVE_BUCKETS: Final[tuple[tuple[int, int | None, int], ...]] = (
        (4000, None, 6000),  # > 4000 W → charge 6000 (top, BMS practical max)
        (3000, 4000, 5000),  # charge 5000 (meter ~-1500 w środku)
        (2000, 3000, 4000),
        (1000, 2000, 3000),
        (0, 1000, 2000),
        (-1000, 0, 1000),  # discharge zbliża się; pv_avail ≤ -1000 → AUTO
    )
    # Low BMS shortcut — BMS clamp na ~2 kW, lookup zbędny.
    LOW_BMS_XSET_W: Final[int] = 3500

    @classmethod
    def entry_block_reason(cls, state: InputState) -> str | None:
        """Reason if entry blocked, else None (entry allowed).

        Pre-charge window blokuje POSITIVE — BatteryManager rządzi.
        NEGATIVE może działać w pre_charge (osobna strategia).
        """
        if cls._is_in_pre_charge_window(state):
            return "in_pre_charge_window"
        if state.exported_energy_hourly <= cls.BALANCE_GATE_KWH:
            return "balance_below_threshold"
        if state.battery_soc >= cls.SOC_ENTRY_CEILING:
            return "soc_at_entry_ceiling"
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

    @classmethod
    def exit_reason(cls, state: InputState) -> str | None:
        """Reason if exit fires, else None (continue)."""
        if cls._is_in_pre_charge_window(state):
            return "in_pre_charge_window"
        if state.exported_energy_hourly < cls.EXIT_BALANCE_KWH:
            return "balance_recovered"
        if state.battery_soc >= cls.SOC_CEILING:
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
    def _is_in_pre_charge_window(cls, state: InputState) -> bool:
        """Pre-charge: 7:00 ≤ now < start_charge_hour_override.

        Multi-caller helper (entry_block_reason + exit_reason) — umieszczone
        zaraz po ostatnim caller-ze (exit_reason).
        """
        if state.start_charge_hour_override is None:
            return False
        if state.now.hour < cls.PRE_CHARGE_WINDOW_START_HOUR:
            return False
        return state.now.time() < state.start_charge_hour_override

    @classmethod
    def apply(
        cls, state: InputState, current_xset: int | None
    ) -> tuple[str | None, int | None, str]:
        """Compute (mode, xset, reason) dla POSITIVE intervention.

        `mode is None` sygnalizuje exit (orchestrator robi `_set_neutral(reason)`).
        Inne mode → orchestrator ustawia recommended_*.

        Kolejność:
        1. STANDBY (najwyższy priorytet) — pv_for_standby < 200W
        2. Low BMS shortcut — battery_charge_limit ≤ 7A
        3. charge_adaptive z hysteresis — fallback do AUTO przy pv_avail ≤ -1000
        """
        # 1. PV niskie → STANDBY (chwilowy pv_power flapuje, używamy avg 2min;
        # fallback do chwilowego gdy avg=None — np. po restart HA).
        pv_for_standby = (
            state.pv_power_avg_2_minutes
            if state.pv_power_avg_2_minutes is not None
            else state.pv_power
        )
        if pv_for_standby < cls.PV_STANDBY_THRESHOLD_W:
            return (cls.STANDBY_MODE, 0, "low_pv_standby")

        # 2. Low BMS shortcut — bateria clamp ~2 kW, lookup zbędny.
        # NIE wymaga pv_available, więc shortcut przed guard'em.
        if (
            state.battery_charge_limit is not None
            and state.battery_charge_limit <= cls.BMS_LOW_LIMIT_A
        ):
            return (
                cls.CHARGE_MODE,
                cls.LOW_BMS_XSET_W,
                f"charge_adaptive_low_bms_{cls.LOW_BMS_XSET_W}W",
            )

        # 3. charge_adaptive lookup — wymaga pv_available.
        if state.pv_available is None:
            # Exit signal — orchestrator robi _set_neutral.
            return (None, None, "none_pv_available")

        pv_available = state.pv_available

        # Hysteresis — current Xset stable jeśli pv_available w rozszerzonym range.
        current_range = cls._xset_range(current_xset)
        if current_range is not None:
            lower, upper = current_range
            if (lower - cls.HYSTERESIS_W) < pv_available <= (upper + cls.HYSTERESIS_W):
                return (
                    cls.CHARGE_MODE,
                    current_xset,
                    f"charge_adaptive_stay_{current_xset}W_pv_avail_{int(pv_available)}",
                )

        # Fresh lookup
        xset = cls._lookup_xset(pv_available)
        if xset is not None:
            return (
                cls.CHARGE_MODE,
                xset,
                f"charge_adaptive_{xset}W_pv_avail_{int(pv_available)}",
            )

        # pv_available ≤ -1000 → mode=AUTO ale stay in intervention (NIE exit).
        # block_discharge w battery.py przejmuje gdy hourly idzie negative.
        return (
            cls.AUTO_MODE,
            None,
            f"charge_adaptive_auto_pv_avail_{int(pv_available)}",
        )

    @classmethod
    def _xset_range(cls, xset: int | None) -> tuple[float, float] | None:
        """Range pv_available który aktywowałby dany Xset.

        Zwraca (lower, upper) lub None gdy xset nie jest w ADAPTIVE_BUCKETS
        (np. low_bms_shortcut 3500 — fallback do plain lookup).
        Najwyższy bucket ma upper=inf.
        """
        if xset is None:
            return None
        for lower, upper, xs in cls.ADAPTIVE_BUCKETS:
            if xs == xset:
                upper_f = float("inf") if upper is None else float(upper)
                return (float(lower), upper_f)
        return None

    @classmethod
    def _lookup_xset(cls, pv_available: float) -> int | None:
        """Znajdź Xset dla pv_available z ADAPTIVE_BUCKETS.

        Returns None gdy pv_available ≤ -1000 (poza najniższym bucket'em) —
        orchestrator przełącza na AUTO mode (stay in intervention).
        """
        for lower, upper, xset in cls.ADAPTIVE_BUCKETS:
            if upper is None:
                if pv_available > lower:
                    return xset
            elif lower < pv_available <= upper:
                return xset
        return None
