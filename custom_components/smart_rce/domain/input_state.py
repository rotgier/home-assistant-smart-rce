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
    exported_energy_hourly: float | None = None
    heater_mode: str | None = None
    depth_of_discharge: float | None = (
        None  # % (number.goodwe_depth_of_discharge_on_grid)
    )
    now: datetime | None = None
