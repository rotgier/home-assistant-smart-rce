"""Shared InputState — snapshot of HA entities read by EMS managers.

Wydzielone do osobnego pliku żeby battery.py i water_heater.py mogły
importować InputState bez circular dependency z ems.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time


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
    heater_mode: str | None = None
    depth_of_discharge: float | None = (
        None  # % (number.goodwe_depth_of_discharge_on_grid)
    )
    battery_charge_toggle_on: bool | None = None
    # input_boolean.battery_charge_max_current_toggle state.
    # Używane przez BatteryManager jako guard dla block_charge (nie blokuj
    # ładowania gdy już zablokowane).

    ems_allow_discharge_override: bool | None = None
    # input_boolean.ems_allow_discharge_override — manualny override.
    # Gdy True, BatteryManager "stoi z boku" (oba should_block_* = False).
    # Używane przy intencjonalnym rozładowaniu (automation Battery Discharge
    # Max at 8) żeby EMS nie blokował export'u.

    start_charge_hour_override: time | None = None
    # input_datetime.rce_start_charge_hour_today_override (HH:MM:SS).
    # Pre-charge window end. Domyślnie kopia rce_start_charge_hour_today_time,
    # user może nadpisać ręcznie.

    water_heater_strategy: str | None = None
    # input_select.ems_water_heater_strategy — strategia rezerwacji PV między
    # baterią a grzałkami w trybie BALANCED. Opcje: NORMAL (domyślny algorytm),
    # BATTERY_FIRST (reserved=4500 gdy battery_charge_limit>7).

    rce_should_hold_for_peak: bool | None = None
    # binary_sensor.rce_should_hold_for_peak (HA template) — True gdy
    # max(evening today 19-22, morning tomorrow 6-9) brutto > threshold.
    # W afternoon window (13-19): True → status quo (DoD=0 z automation),
    # False → BatteryManager dynamic block_discharge na avg_5min + exported_wh.

    is_workday: bool | None = None
    # binary_sensor.workday (HA workday integration, country=PL).
    # True=workday (Pn-Pt bez świąt), False=weekend/święto.
    # W weekend BatteryManager nie steruje block_discharge w pre-charge i
    # post-charge (passthrough block_discharge=False) — RCE typowo płaski,
    # brak drogich godzin do ochrony surplus PV. Block_charge i
    # afternoon-dynamic bez zmian.

    pv_power: float | None = None
    # sensor.pv_power (W) — chwilowa moc PV (DC). Używane przez
    # GridExportManager do progu STANDBY (<200W → bateria stop).

    battery_power_max_18s: float | None = None
    # sensor.battery_power_max_18s (W, statistics value_max max_age 18s).
    # Konwencja: charging = NEGATIVE (bateria pobiera moc). max z 18s = wartość
    # NAJBLIŻSZA ZERA (najmniej intensywne ładowanie w okresie).
    # Próg < -2500 W → bateria stale ładuje się ≥ 2.5 kW (intensywne).
    # Próg < -4900 W → bateria blisko BMS cap (5.5 kW), PV cięcie zaraz.
    # Używane przez GridExportManager state machine.

    meter_active_power_total_max_18s: float | None = None
    # sensor.meter_active_power_total_max_18s (W, statistics value_max max_age 18s).
    # Konwencja: import = NEGATIVE (z grida do domu). max z 18s = wartość
    # NAJBLIŻSZA ZERA (najmniej intensywny import).
    # Próg < -3900 W → meter stale importuje ≥ 3.9 kW (CHARGE za agresywne).
    # Używane przez GridExportManager state machine do switch CHARGE→BUY.

    goodwe_ems_mode: str | None = None
    # select.goodwe_ems_mode — aktualny tryb EMS Goodwe (auto, charge_battery,
    # battery_standby, sell_power, etc.). Diagnostic.

    other_ems_automation_active_this_hour: bool | None = None
    # binary_sensor.ems_other_automation_active_this_hour (HA template).
    # True gdy któraś z innych automatyzacji EMS (battery_charge_*,
    # battery_discharge_*) odpaliła się w bieżącej godzinie. Używane przez
    # GridExportManager jako entry gate.

    grid_export_strategy_mode: str | None = None
    # input_select.smart_rce_grid_export_strategy_mode — runtime kontrola
    # GridExportManager. Opcje: "disabled" (intervention off, manager
    # diagnostuje would-be), "charge_or_standby" (tylko CHARGE_BATTERY 6000
    # lub STANDBY, bez BUY_POWER), "all" (pełny state machine).
    # Defensive: gdy None → manager traktuje jako "disabled" (safe default).

    block_charge_logic_mode: str | None = None
    # input_select.smart_rce_block_charge_logic_mode — runtime kontrola
    # BatteryManager.should_block_battery_charge. Opcje: "enabled" (default,
    # current behavior), "disabled" (block_charge zawsze False, toggle nie
    # jest wyłączane).
    # Defensive: gdy None lub "enabled" → block_charge logika włączona.

    now: datetime | None = None
