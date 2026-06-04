"""Water Heater Manager — CWU heater on/off decisions.

Sterowanie grzałkami CWU (BIG 3kW, SMALL 1.5kW) — BALANCED-only logic:
- PV surplus (sensor minus_pv)
- SOC baterii + battery_charge_limit (ile bateria przyjmie)
- Aktualna interwencja GridExportManager (POSITIVE/NEGATIVE — większy reserved)
- User override `prefer_battery_first` (heaters only run when bonus is meaningful)

Two-tier decision:
1. Baseline — co PV samo wystarcza po odjęciu `reserved` per battery_charge_limit
   + intervention
2. Upgrade — adaptive lift to consume the hourly export bonus (so we don't
   waste already-exported Wh)

NEGATIVE intervention wymusza większy reserved (grzałki off priorytetowo) bo
grzałka 3kW jest typowo główną przyczyną deficytu hourly.

`prefer_battery_first` override: gdy True, user chce maxować ładowanie baterii.
Grzałki mogą się włączyć TYLKO gdy export_bonus pokona próg (≥1000W; trzyma
w hysteresis ≥500W) — czyli realnie odzyskujemy wyeksportowane Wh, nie firujemy
grzałek "z samej baseline PV". Plus reserved escaluje do max battery capability
per tier (2000W przy >2, 600W przy ==2). Wyjątek: POSITIVE intervention nie
escalates (bateria już dostaje surplus przez interwencję, grzałki też mogą).
"""

from __future__ import annotations

from datetime import datetime

from custom_components.smart_rce.domain.grid_export import InterventionDirection
from custom_components.smart_rce.domain.input_state import InputState

# Adaptacyjny upgrade pod budżet eksportu w resztę godziny.
# Bonus = exported_energy_so_far / czas_do_końca_godziny — przelicza dotąd
# wyeksportowane Wh na ekwiwalent dodatkowej mocy dostępnej do dożarcia.
EXPORT_BONUS_CUTOFF_SEC: int = 60  # < tego nie aktywuj bonusa (ostatnia minuta)
EXPORT_BONUS_MIN_T_LEFT_SEC: int = 60  # clamp dolny dla dzielenia
EXPORT_BONUS_HYSTERESIS_W: int = 500

# Mode-specific bonus gate (only active when prefer_battery_first=True).
# Gate opens at ≥1000W (real export to recover); held open down to 500W via
# hysteresis. Below threshold w trybie battery-first → heaters OFF.
BONUS_GATE_ON_W: int = 1000
BONUS_GATE_OFF_W: int = 500


def seconds_until_hour_end(now: datetime) -> int:
    return 3600 - (now.minute * 60 + now.second)


class WaterHeaterManager:
    BIG_POWER: int = 3000
    SMALL_POWER: int = 1500
    BOTH_POWER: int = 4500

    BOTH_ARE_ON: str = "both_are_on"
    BIG_IS_ON: str = "big_is_on"
    SMALL_IS_ON: str = "small_is_on"
    BOTH_ARE_OFF: str = "both_are_off"

    _STATE_ORDER: dict[str, int] = {
        "both_are_off": 0,
        "small_is_on": 1,
        "big_is_on": 2,
        "both_are_on": 3,
    }

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
        # Diagnostics (heater_* prefix — was balanced_* before BALANCED-only refactor)
        self.heater_budget: float | None = None
        self.heater_baseline: str | None = None
        self.heater_upgrade_target: str | None = None
        self.heater_upgrade_active: bool = False
        self.heater_export_bonus: float | None = None
        # True when prefer_battery_first=True AND bonus gate is open
        # (bonus≥1000W or held by hysteresis ≥500W). Signals "heater
        # allowed to run despite battery-first preference because real
        # export bonus needs recovering".
        self.heater_running_via_bonus: bool = False

    def update(
        self,
        state: InputState,
        grid_export_intervention: InterventionDirection | None = None,
        *,
        battery_charge_allowed: bool,
        reserved_balanced_full: int = 5500,
        prefer_battery_first: bool = False,
    ) -> None:
        """Update target state based on PV/battery/heater config.

        `grid_export_intervention` (POSITIVE/NEGATIVE/None):
        - POSITIVE: bateria łapie surplus, reserved zwiększony do 3500W
          (`charge_limit > 7`) by chronić baterię intervention.
        - NEGATIVE: deficit hourly — większy reserved (5500W dla `>7`,
          2000W dla `>2`, 600W dla `==2`) by wymusić grzałki off.
        - None: original logic.

        `battery_charge_allowed`: kwarg from BatteryChargeService — replaces
        legacy `state.battery_charge_toggle_on`. When False, effective
        charge_limit is treated as 0 (battery idle, PV fully available
        for heaters).

        `prefer_battery_first`: user-controlled override from
        `switch.ems_water_heater_prefer_battery_first`. When True:
        - Reserved escalates to max battery capability per tier (>7: 5500;
          >2: 2000; ==2: 600) — except under POSITIVE intervention.
        - At `battery_charge_limit > 2`: bonus gate applies. Heaters fire
          ONLY when export_bonus ≥1000W (or ≥500W with hysteresis when
          currently on). Otherwise target=OFF. When gate open:
          target=max(baseline, upgrade).
        - At `battery_charge_limit <= 2`: gate IGNORED (legacy semantic).
          Battery near-full, reserved (300/600) covers modest demand, no
          heater-vs-battery conflict. Baseline fires normally.
        - At `battery_charge_limit == 1`/`== 0`: reserved escalation
          skipped (stays at 300/0 — no battery draw to escalate to).
        """
        if self._none_present(state):
            return

        current_state = self._current_state(state)
        target = self.target(
            state,
            current_state,
            grid_export_intervention=grid_export_intervention,
            battery_charge_allowed=battery_charge_allowed,
            reserved_balanced_full=reserved_balanced_full,
            prefer_battery_first=prefer_battery_first,
        )

        self.should_turn_on = target in (self.BIG_IS_ON, self.BOTH_ARE_ON)
        self.should_turn_off = target in (self.SMALL_IS_ON, self.BOTH_ARE_OFF)
        self.should_turn_on_small = target in (self.SMALL_IS_ON, self.BOTH_ARE_ON)
        self.should_turn_off_small = target in (self.BIG_IS_ON, self.BOTH_ARE_OFF)

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

    def _current_state(self, state: InputState) -> str:
        if state.water_heater_big_is_on and state.water_heater_small_is_on:
            return self.BOTH_ARE_ON
        if state.water_heater_big_is_on:
            return self.BIG_IS_ON
        if state.water_heater_small_is_on:
            return self.SMALL_IS_ON
        return self.BOTH_ARE_OFF

    def target(
        self,
        state: InputState,
        current_state: str,
        *,
        grid_export_intervention: InterventionDirection | None = None,
        battery_charge_allowed: bool = True,
        reserved_balanced_full: int = 5500,
        prefer_battery_first: bool = False,
    ) -> str:
        """Pure decision — BALANCED two-tier logic with optional battery-first override.

        Two tiers:
        1. Baseline — heater_budget = pv - reserved. Reserved scales with
           battery_charge_limit + intervention + prefer_battery_first.
        2. Upgrade — effective_budget = heater_budget + export_bonus_w.
           Lifts target above baseline to consume already-exported Wh in
           the rest of the current hour.

        Default mode (`prefer_battery_first=False`): target = max(baseline,
        upgrade_candidate). 500W hysteresis on the ladder prevents noise flap.

        Battery-first mode (`prefer_battery_first=True`):
        - Reserved escalates to max battery capability per tier (see
          `high_reserve` helper below), except POSITIVE intervention which
          stays at modest reserved (battery already gets surplus).
        - Bonus gate filters small noise-bonus: heaters fire ONLY when
          export_bonus ≥ 1000W (or ≥500W with hysteresis when currently on).
          Otherwise target=OFF.
        - When gate open: target = max(baseline, upgrade_candidate) — fixes
          the edge case where baseline saturates at BOTH_ARE_ON and the
          override would have suppressed heaters.
        """
        pv_available = -state.consumption_minus_pv_2_minutes
        # Effective charge limit captures "is battery actively absorbing PV right now?"
        # When battery_charge_allowed=False (user disabled charging, e.g. pre-charge
        # window before scheduled charge start), treat as 0 regardless of BMS hardware
        # cap. Source of truth = BatteryChargeService.charge_allowed (also used by
        # positive/negative.py for the same "is the inverter actually charging"
        # semantic). BMS limit fallback when allowed.
        battery_charge_limit = (
            0.0 if not battery_charge_allowed else state.battery_charge_limit
        )
        exported_energy = state.exported_energy_hourly * 1000  # kWh → Wh

        # ─── Reserved per battery_charge_limit + intervention + prefer_battery_first ───
        # `==` zamiast `is` — StrEnum compare value-based, odporne na module reload.
        # Po `live_reload()` water_heater może trzymać OLD InterventionDirection
        # class reference (reloaded przed grid_export), `is` fail mimo same value.
        is_positive = grid_export_intervention == InterventionDirection.POSITIVE
        is_negative = grid_export_intervention == InterventionDirection.NEGATIVE

        # high_reserve: escalate reserved to max battery capability per tier.
        # Negative intervention always escalates (force heaters off). Battery-first
        # also escalates EXCEPT under positive intervention (battery already
        # getting surplus via the intervention; heaters can also consume).
        high_reserve = (is_negative or prefer_battery_first) and not is_positive

        if battery_charge_limit > 7:
            if is_positive:
                reserved = 3500
            elif high_reserve:
                reserved = 5500  # battery max draw; grzałki MUSZĄ off
            else:
                reserved = reserved_balanced_full
        elif battery_charge_limit > 2:
            reserved = 2000 if high_reserve else 1000
        elif battery_charge_limit == 2:
            reserved = 600 if high_reserve else 300
        elif battery_charge_limit == 1:
            reserved = 300
        else:  # battery_charge_limit == 0
            reserved = 0

        heater_budget = pv_available - reserved
        hysteresis = 500

        # ─── Piętro 1 — Baseline (histereza trzyma tylko obecny stan) ───
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

        # ─── Piętro 2 — adaptive upgrade ───
        # Skip gdy battery_charge_limit > 7 (bateria chce max mocy ~5.2 kW).
        # Instalacja PV 9.1 kW, grzałki max 4.5 kW, bateria max 5.2 kW — suma
        # 9.7 kW > PV. Gdy bateria chce max, każdy włączony watt grzałki jest
        # zabrany baterii (Goodwe rebalansuje automatycznie).
        #
        # `prefer_battery_first=True` overrides skip — user akceptuje że grzałki
        # kradną part of battery charge gdy export_bonus uzasadnia (gate open).
        #
        # Bonus: ile dodatkowej mocy "musi być zjedzone" w resztę godziny,
        # żeby zniwelować dotąd wyeksportowane Wh. **Brak cap** — bonus może
        # przekraczać BOTH_POWER (drabinka i tak limit na BOTH_ARE_ON). Cap
        # poprzednio blokował aktywację gdy heater_budget ujemny + duży
        # eksport w końcówce godziny (scenariusz: 5 min do końca, 4.5kWh
        # wyexportowane, heater_budget=-2500 — bez cap effective=51500
        # → BOTH_ARE_ON; z cap effective=2000 → tylko SMALL).
        # W ostatnich EXPORT_BONUS_CUTOFF_SEC sekundach nie aktywujemy bonusa
        # (i tak nie zdążymy zjeść; uniknięcie ostatniego szarpnięcia przed
        # resetem utility_meter).
        skip_upgrade = battery_charge_limit > 7 and not prefer_battery_first
        seconds_left = seconds_until_hour_end(state.now)
        if seconds_left >= EXPORT_BONUS_CUTOFF_SEC and not skip_upgrade:
            t_left_h = max(seconds_left, EXPORT_BONUS_MIN_T_LEFT_SEC) / 3600
            export_bonus = max(0.0, exported_energy / t_left_h)
        else:
            export_bonus = 0.0

        effective_budget = heater_budget + export_bonus
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

        # ─── Resolve final target ───
        # Default mode: target = max(baseline, upgrade_candidate). 500W upgrade
        # hysteresis prevents noise-driven flap.
        #
        # Battery-first mode (only_when battery_charge_limit > 2): mode-specific
        # bonus gate filters small bonuses. When gate is closed → heaters OFF.
        # When open → max(baseline, upgrade). Hysteresis 1000W/500W: gate opens
        # at ≥1000W, holds open down to ≥500W when heaters currently running.
        #
        # At battery_charge_limit <= 2 override IGNORED (legacy semantic) —
        # battery near-full, reserved alone (300/600) covers modest demand,
        # no battery-vs-heater conflict at low tiers.
        override_applies = prefer_battery_first and battery_charge_limit > 2
        bonus_gate_open = export_bonus >= BONUS_GATE_ON_W or (
            current_state != self.BOTH_ARE_OFF and export_bonus >= BONUS_GATE_OFF_W
        )
        heater_running_via_bonus = override_applies and bonus_gate_open
        upgrade_beats_baseline = (
            self._STATE_ORDER[upgrade_candidate] > self._STATE_ORDER[baseline]
        )

        if override_applies and not bonus_gate_open:
            target = self.BOTH_ARE_OFF
        elif upgrade_beats_baseline:
            target = upgrade_candidate
        else:
            target = baseline

        # ─── Diagnostics ───
        self.heater_budget = -heater_budget
        self.heater_baseline = baseline
        self.heater_upgrade_active = target != baseline
        if target == baseline:
            self.heater_upgrade_target = f"{self._STATE_LABELS[baseline]} (baseline)"
        else:
            self.heater_upgrade_target = (
                f"{self._STATE_LABELS[baseline]} -> {self._STATE_LABELS[target]}"
            )
        self.heater_export_bonus = export_bonus
        self.heater_running_via_bonus = heater_running_via_bonus

        return target
