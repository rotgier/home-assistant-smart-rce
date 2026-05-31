"""Shared InputState — snapshot of HA entities read by EMS managers.

Wydzielone do osobnego pliku żeby battery.py i water_heater.py mogły
importować InputState bez circular dependency z ems.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class InputState:
    water_heater_big_is_on: bool | None = None
    water_heater_small_is_on: bool | None = None

    battery_soc: float | None = None
    battery_charge_limit: float | None = None  # A (ampery z BMS)
    battery_power_2_minutes: float | None = None
    consumption_minus_pv_2_minutes: float | None = None
    consumption_minus_pv_5_minutes: float | None = None
    # sensor.house_consumption_minus_heaters_minus_pv_avg_5_minutes (W).
    # Ujemne = PV > cons (surplus trend). Używane w post-charge dla
    # continuous check block_discharge (hysteresis -500/0).
    exported_energy_hourly: float | None = None
    depth_of_discharge: float | None = (
        None  # % (number.goodwe_depth_of_discharge_on_grid)
    )
    # `battery_charge_toggle_on` REMOVED (Etap B) — replaced by
    # `BatteryChargePolicy.charge_allowed` (smart_rce-managed select
    # `select.ems_battery_charge_allowed_override`). Read in
    # `Ems.update_state` from BatteryChargeService, passed explicitly as
    # keyword argument `battery_charge_allowed` to
    # `GridExportManager.update` and `WaterHeaterManager.update`
    # (mirrors `ems_interventions_blocked` Etap 0 pattern).

    # `ems_allow_discharge_override` REMOVED (Etap 0) — replaced by
    # `BatterySchedule.ems_interventions_blocked` (smart_rce-managed switch
    # `switch.ems_interventions_blocked`). Same kwarg-passing pattern as
    # `battery_charge_allowed`.

    # `start_charge_hour_override` REMOVED (Etap B'-2) — replaced by
    # `BatteryChargePolicy.start_charge_hour_override` (smart_rce-managed
    # `time.ems_battery_charge_start_hour_override` entity). Read in
    # `Ems.update_state` from BatteryChargeService property, passed explicitly
    # as keyword argument to `DodPolicy.update` and `GridExportManager.update`
    # (mirrors `ems_interventions_blocked` Etap 0 + `battery_charge_allowed`
    # Etap B patterns).

    # `should_hold_for_peak` REMOVED — was previously read from HA
    # template `binary_sensor.rce_should_hold_for_peak`, but smart_rce
    # already owns `discharge_slots.max_upcoming_peak` so it's a 6-hop
    # round-trip through HA for a value computable locally. Now Ems
    # computes `peak.price * GROSS_MULTIPLIER > rce_high_price_threshold_gross`
    # and passes the result as a kwarg to `DodPolicy.update`. The HA
    # template binary_sensor is kept for legacy automations + dashboards.

    is_workday: bool | None = None
    # binary_sensor.workday (HA workday integration, country=PL).
    # True=workday (Pn-Pt bez świąt), False=weekend/święto.
    # W weekend BatteryManager nie steruje block_discharge w pre-charge i
    # post-charge (passthrough block_discharge=False) — RCE typowo płaski,
    # brak drogich godzin do ochrony surplus PV. Block_charge i
    # afternoon-dynamic bez zmian.

    pv_power: float | None = None
    # sensor.pv_power (W) — chwilowa moc PV (DC). Diagnostic + fallback dla
    # pv_power_avg_2_minutes gdy avg jeszcze nie zebrał próbek.

    pv_power_avg_2_minutes: float | None = None
    # sensor.pv_power_avg_2_minutes (W, statistics mean max_age 2min) —
    # uśredniona moc PV używana przez GridExportManager dla progu STANDBY
    # (<200W → bateria stop). Avg eliminuje flap'owanie gdy inwerter
    # chwilowo "przymuli się" (transient spadek <200W).

    goodwe_ems_mode: str | None = None
    # select.goodwe_ems_mode — aktualny tryb EMS Goodwe (auto, charge_battery,
    # battery_standby, sell_power, etc.). Diagnostic.

    grid_export_strategy_mode: str | None = None
    # input_select.smart_rce_grid_export_strategy_mode — runtime kontrola
    # GridExportManager. Opcje: "disabled" (intervention off, manager
    # diagnostuje would-be), "charge_adaptive" (domyślne aktywne — STANDBY
    # gdy PV<200W, lookup-based Xset gdy PV≥200W).
    # Defensive: gdy None → manager traktuje jako "disabled" (safe default).

    heater_rce_threshold: float | None = None
    # input_number.heater_rce_threshold (net zł/MWh) — RCE price below which
    # heater automations may turn ON. Mirrors automation conditions on
    # `sensor.rce_current_price`. ChargeSlots uses it to skip
    # shift_earlier_if_cheap on days where heaters are effectively blocked
    # (sink for surplus PV reduces to battery capacity → no benefit from
    # widening charge window).

    dod_override: float | None = None
    # input_number.ems_dod_override (range -1..100; -1 = inactive).
    # When ≥0, DodPolicy emits this value as target_dod (highest priority
    # except ems_interventions_blocked which is passed as kwarg).
    # Auto-expires on phase boundary.

    is_workday_tomorrow: bool | None = None
    # binary_sensor.workday_tomorrow (HA workday integration, country=PL).
    # Used by DodPolicy night-preserve phase (22:00..07:00) to decide whether
    # to preserve battery (workday tomorrow → True → preserve) or allow free
    # discharge.

    rce_high_price_threshold_gross: float | None = None
    # input_number.rce_high_price_threshold_gross (gr/kWh, default ~350) —
    # threshold above which morning_discharge_price triggers preserve.

    now: datetime | None = None

    @property
    def pv_available(self) -> float | None:
        """Surplus PV ponad dom_bez_heaters (W) — avg 2 min. None gdy sensor unavailable.

        Liczone jako `-consumption_minus_pv_2_minutes`:
        - dodatnie = PV > dom_bez_heaters (surplus, hourly POSITIVE side)
        - ujemne   = dom_bez_heaters > PV (deficit, hourly NEGATIVE side)

        Używane przez GridExportManager (charge_adaptive lookup, NEGATIVE buckets).
        """
        if self.consumption_minus_pv_2_minutes is None:
            return None
        return -self.consumption_minus_pv_2_minutes

    @property
    def pv_available_5min(self) -> float | None:
        """Surplus PV ponad dom_bez_heaters (W) — avg 5 min. None gdy unavailable.

        Wariant `pv_available` z dłuższym oknem uśredniania. Używane przez
        BatteryManager w post-charge i afternoon-dynamic dla sustained
        trend check (eliminuje noise 2-min, np. cykl lodówki).

        - dodatnie = PV > dom_bez_heaters (surplus)
        - ujemne   = dom_bez_heaters > PV (deficit)
        """
        if self.consumption_minus_pv_5_minutes is None:
            return None
        return -self.consumption_minus_pv_5_minutes
