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
    exported_energy_hourly: float | None = None
    heater_mode: str | None = None
    depth_of_discharge: float | None = (
        None  # % (number.goodwe_depth_of_discharge_on_grid)
    )
    battery_charge_toggle_on: bool | None = None
    # input_boolean.battery_charge_max_current_toggle state.
    # Używane przez BatteryManager jako guard dla block_charge (nie blokuj
    # ładowania gdy już zablokowane).

    start_charge_hour_override: time | None = None
    # input_datetime.rce_start_charge_hour_today_override (HH:MM:SS).
    # Pre-charge window end. Domyślnie kopia rce_start_charge_hour_today_time,
    # user może nadpisać ręcznie.

    now: datetime | None = None
