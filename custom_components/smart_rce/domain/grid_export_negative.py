"""NEGATIVE strategy — adaptive charge/discharge buckets, target meter ≈ +1500W eksport.

Aktywowana gdy hourly bilans negatywny (import netto). Replaces YAML automation
`Inverter grid export to avoid NEGATIVE balance` (Sell Power 1500W) — Sell Power
nie liczył grid side load (obs. zmywarka 1.5kW + Sell Power 1500W = faktyczny
eksport 100-200W zamiast 1500W).

Strategia: 10 bucketów na `pv_available` (= PV − dom_bez_heaters). Każdy bucket
mapuje pv_available na signed xset:
- xset > 0  → CHARGE_BATTERY (PV nadwyżka, ładuj baterię, eksport ~1500W)
- xset = 0  → DISCHARGE_BATTERY xset=0 (bucket STOP, bateria stoi, eksport = pv_avail)
- xset < 0  → DISCHARGE_BATTERY (deficit PV, oddaj z baterii, eksport ~1500W)

Hysteresis ±300W na granicach bucketów (analog POSITIVE). SoC clamp:
- bucket DISCHARGE wymaga SoC > min_soc (= 100 - DoD)
- bucket CHARGE + SoC=100 → clamp do bucket STOP (eksport z PV niweluje NEGATIVE)
- bucket STOP zawsze feasible

Pre_charge window NIE blokuje (POSITIVE skip, NEGATIVE pozwolony — bateria może
discharge'ować jeśli SoC > min). `ems_allow_discharge_override=True` blokuje
NEGATIVE entry/continue (user wymusza discharge — np. Battery Discharge Max).
"""

from __future__ import annotations

from typing import Final

from custom_components.smart_rce.domain.input_state import InputState


class NegativeStrategy:
    """Strategy dla NEGATIVE balance — entry gates, exit gates, adaptive buckets."""

    # --- entry/exit thresholds ---
    # Pre-45min: -0.05 (toleruj umiarkowane negative, czas na natural recovery z PV).
    # Post-45min: 0.0 (każdy negative — godzina się kończy).
    ENTRY_THRESHOLD_EARLY_KWH: Final[float] = -0.05
    ENTRY_THRESHOLD_LATE_KWH: Final[float] = 0.0
    EXIT_BALANCE_KWH: Final[float] = 0.0
    LATE_HALF_HOUR_MINUTE: Final[int] = 45

    # SoC floors / ceilings
    SOC_HARD_FLOOR: Final[int] = 10
    SOC_CEILING: Final[int] = 100  # bucket charge clamp (= bateria pełna)

    # Time gates (w obrębie godziny)
    LATE_HOUR_MINUTE: Final[int] = 59
    LATE_HOUR_SECOND: Final[int] = 40
    EXIT_END_OF_HOUR_MINUTE: Final[int] = 59
    EXIT_END_OF_HOUR_SECOND: Final[int] = 50

    # Hysteresis dla bucket transitions
    HYSTERESIS_W: Final[int] = 300

    # Mode constants (Goodwe EMS)
    STANDBY_MODE: Final[str] = "discharge_battery"  # bucket STOP (xset=0)
    DISCHARGE_MODE: Final[str] = "discharge_battery"  # bucket DISCHARGE (xset>0)
    CHARGE_MODE: Final[str] = "charge_battery"  # bucket CHARGE

    # Adaptive buckets — `(lower, upper, xset_signed)`.
    # Aktywuje się gdy `lower < pv_available <= upper` (najwyższy bucket: upper=None=+inf).
    # Bucket centrum daje eksport ~1500W (Xset = lower - 1000).
    ADAPTIVE_BUCKETS: Final[tuple[tuple[int, int | None, int], ...]] = (
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

    @classmethod
    def entry_block_reason(cls, state: InputState) -> str | None:
        """Reason if entry blocked, else None (entry allowed).

        Filozofia: entry tylko gdy bucket może realnie wpłynąć na saldo.
        - bucket DISCHARGE wymaga SoC > min_soc (energia do oddania)
        - bucket CHARGE + SoC=100 → entry pozwolony (clamp do bucket STOP)
        - bucket STOP zawsze feasible

        EMS override (`ems_allow_discharge_override=True`) blokuje NEGATIVE — user
        wymusza discharge (np. Battery Discharge Max), nie ingerujemy.
        """
        if state.ems_allow_discharge_override is True:
            return "ems_allow_discharge_override"
        threshold = cls._entry_threshold(state)
        if state.exported_energy_hourly >= threshold:
            return f"balance_above_neg_threshold_{threshold:.2f}"
        if state.battery_soc <= cls.SOC_HARD_FLOOR:
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
        xset_signed = cls._lookup_xset(state.pv_available)
        if xset_signed < 0 and state.battery_soc <= (100 - state.depth_of_discharge):
            return "soc_at_dod_floor_no_discharge"
        return None

    @classmethod
    def _entry_threshold(cls, state: InputState) -> float:
        """Time-dependent entry threshold (-0.05 pre-45min, 0.0 post-45min)."""
        if state.now.minute < cls.LATE_HALF_HOUR_MINUTE:
            return cls.ENTRY_THRESHOLD_EARLY_KWH
        return cls.ENTRY_THRESHOLD_LATE_KWH

    @classmethod
    def exit_reason(cls, state: InputState, current_xset_signed: int) -> str | None:
        """Reason if exit fires, else None (continue).

        `current_xset_signed` = xset PO clamp_charge_bucket (orchestrator passes
        post-clamp value). Bucket charge + SoC=100 jest już clamp'owany do 0,
        więc tutaj widzimy tylko discharge (xset<0) lub stop (xset=0).
        """
        if state.ems_allow_discharge_override is True:
            return "ems_allow_discharge_override"
        if state.exported_energy_hourly > cls.EXIT_BALANCE_KWH:
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

    @classmethod
    def resolve_for_continue(
        cls,
        state: InputState,
        current_mode: str,
        current_xset: int | None,
    ) -> tuple[int, bool, float] | None:
        """Resolve dla continue path — hysteresis-aware.

        Utrzymuje current bucket gdy pv_available oscyluje na granicy. Flow:
        signed_xset z (mode, xset) → hysteresis lookup → SoC clamp.

        Returns None gdy `state.pv_available is None`.
        """
        if state.pv_available is None:
            return None
        pv_available = state.pv_available
        current_signed = cls._signed_xset(current_mode, current_xset)
        xset_signed, is_stay = cls._resolve_xset_with_hysteresis(
            pv_available, current_signed
        )
        xset_signed, is_stay = cls._clamp_charge_bucket(xset_signed, is_stay, state)
        return xset_signed, is_stay, pv_available

    @classmethod
    def _signed_xset(cls, mode: str, xset: int | None) -> int | None:
        """Aktualny xset_signed z (mode, xset). None gdy auto/idle.

        Konwersja używana przez hysteresis lookup — NEGATIVE buckets są signed
        (xset>0 = charge, xset<0 = discharge), a manager trzyma osobno mode + |xset|.
        """
        if xset is None:
            return None
        if mode == cls.CHARGE_MODE:
            return xset
        if mode == cls.DISCHARGE_MODE:
            # xset=0 → bucket stop; xset>0 → bucket discharge → -xset
            return -xset if xset > 0 else 0
        return None

    @classmethod
    def _resolve_xset_with_hysteresis(
        cls, pv_available: float, current_xset_signed: int | None
    ) -> tuple[int, bool]:
        """Lookup z hysteresis (current bucket + ±300W tolerance).

        Returns (xset_signed, is_stay):
        - is_stay=True gdy hysteresis utrzymał current bucket
        - is_stay=False gdy fresh lookup (zmiana bucketu, lub current poza bucketami)
        """
        current_range = cls._xset_range(current_xset_signed)
        if current_range is not None:
            lower, upper = current_range
            if (lower - cls.HYSTERESIS_W) < pv_available <= (upper + cls.HYSTERESIS_W):
                return current_xset_signed, True  # type: ignore[return-value]
        return cls._lookup_xset(pv_available), False

    @classmethod
    def _xset_range(cls, xset_signed: int | None) -> tuple[float, float] | None:
        """Range pv_available który aktywowałby dany xset_signed.

        Zwraca (lower, upper) lub None gdy xset_signed nie jest w bucketach.
        Najwyższy bucket ma upper=inf.
        """
        if xset_signed is None:
            return None
        for lower, upper, xs in cls.ADAPTIVE_BUCKETS:
            if xs == xset_signed:
                upper_f = float("inf") if upper is None else float(upper)
                return (float(lower), upper_f)
        return None

    @classmethod
    def resolve_for_entry(cls, state: InputState) -> tuple[int, bool, float] | None:
        """Resolve dla entry path — fresh lookup bez hysteresis.

        Wchodzimy z AUTO (clean state — bateria oddawała "nie wiadomo co"),
        nie ma sensu matchować przez hysteresis do tego co było wcześniej
        (np. 2 godziny temu).

        Returns None gdy `state.pv_available is None`.
        """
        if state.pv_available is None:
            return None
        pv_available = state.pv_available
        xset_signed = cls._lookup_xset(pv_available)
        xset_signed, _ = cls._clamp_charge_bucket(xset_signed, False, state)
        return xset_signed, False, pv_available

    @classmethod
    def _lookup_xset(cls, pv_available: float) -> int:
        """Znajdź xset_signed dla pv_available z ADAPTIVE_BUCKETS.

        Multi-caller helper (entry_block_reason, _resolve_xset_with_hysteresis,
        resolve_for_entry) — umieszczone zaraz po ostatnim caller-ze
        (resolve_for_entry).
        """
        for lower, upper, xset_signed in cls.ADAPTIVE_BUCKETS:
            if upper is None:
                if pv_available > lower:
                    return xset_signed
            elif lower < pv_available <= upper:
                return xset_signed
        # Fallback: cap przy najgłębszym bucket (pv_avail ≤ -4000) → -6000
        return cls.ADAPTIVE_BUCKETS[-1][2]

    @classmethod
    def _clamp_charge_bucket(
        cls, xset_signed: int, is_stay: bool, state: InputState
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
        if state.battery_soc is not None and state.battery_soc >= cls.SOC_CEILING:
            return 0, False
        if state.battery_charge_toggle_on is False:
            return 0, False
        return xset_signed, is_stay

    @classmethod
    def build_output(
        cls, xset_signed: int, prefix: str, pv_available: float
    ) -> tuple[str, int, str]:
        """Build (mode, xset, reason) z xset_signed.

        - xset_signed > 0 → charge_battery z xset = xset_signed
        - xset_signed = 0 → discharge_battery z xset = 0 (bucket STOP)
        - xset_signed < 0 → discharge_battery z xset = abs(xset_signed)
        """
        if xset_signed > 0:
            return (
                cls.CHARGE_MODE,
                xset_signed,
                f"{prefix}_charge_{xset_signed}W_pv_avail_{int(pv_available)}",
            )
        if xset_signed == 0:
            return (
                cls.STANDBY_MODE,
                0,
                f"{prefix}_stop_xset_0_pv_avail_{int(pv_available)}",
            )
        return (
            cls.DISCHARGE_MODE,
            abs(xset_signed),
            f"{prefix}_discharge_{abs(xset_signed)}W_pv_avail_{int(pv_available)}",
        )
