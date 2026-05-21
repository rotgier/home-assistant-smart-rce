"""Energy Management System — orchestrator (composition + listeners + RCE lifecycle).

After Etap 0: source of truth for `ems_interventions_blocked` moved from HA
`input_boolean.ems_allow_discharge_override` into `BatterySchedule` (domain
aggregate), persisted via `BatteryScheduleRepository`. `Ems.update_state`
reads it from the repo at start, passes it as keyword argument explicitly to
`GridExportManager.update` and `DodPolicy.update`. After the
`BatteryScheduleService.update` call, the flag is re-read in case the service
just engaged/disengaged a slot mid-tick.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging

from custom_components.smart_rce.application.battery_schedule_service import (
    BatteryScheduleService,
)
from custom_components.smart_rce.domain.battery_schedule import BatteryScheduleInput
from custom_components.smart_rce.domain.charge_slots import (
    DEFAULT_HEATER_RCE_THRESHOLD,
    ChargeSlots,
)
from custom_components.smart_rce.domain.discharge_slots import DischargeSlots
from custom_components.smart_rce.domain.dod_policy import DodPolicy
from custom_components.smart_rce.domain.ems_rce_prices import EmsRcePrices
from custom_components.smart_rce.domain.grid_export import GridExportManager
from custom_components.smart_rce.domain.input_state import InputState
from custom_components.smart_rce.domain.rce import RcePrices
from custom_components.smart_rce.domain.water_heater import WaterHeaterManager
from custom_components.smart_rce.infrastructure.battery_schedule_repository import (
    BatteryScheduleRepository,
)

type CALLBACK_TYPE = Callable[[], None]

_LOGGER = logging.getLogger(__name__)


class Ems:
    def __init__(
        self,
        battery_schedule_repo: BatteryScheduleRepository | None = None,
        battery_schedule_service: BatteryScheduleService | None = None,
    ) -> None:
        # Defaults to None for unit-test convenience (tests instantiate `Ems()`
        # and exercise individual managers like `ems.water_heater.update(...)`
        # without going through `update_state`). Production wiring in
        # `ems_factory.create_ems` always passes both.
        self._listeners: dict[CALLBACK_TYPE, CALLBACK_TYPE] = {}
        # Last InputState — udostępniane infrastructure adapters (logger,
        # diagnostic readers) które potrzebują dostępu do bieżących wartości
        # wejściowych. Promote z prywatnego `_ha` (unused) na publiczny.
        self.last_input_state: InputState | None = None
        self.rce_prices: EmsRcePrices = EmsRcePrices()
        self.charge_slots: ChargeSlots = ChargeSlots()
        self.discharge_slots: DischargeSlots = DischargeSlots()
        self.water_heater: WaterHeaterManager = WaterHeaterManager()
        self.grid_export: GridExportManager = GridExportManager()
        self.dod_policy: DodPolicy = DodPolicy()
        # Public attributes — consumed by entities (switch.ems_interventions_blocked)
        # and dashboard cards via `entry.runtime_data.ems.battery_schedule_*`.
        # Consistency with other ems.<manager> attributes (grid_export, dod_policy).
        self.battery_schedule_repo = battery_schedule_repo
        self.battery_schedule_service = battery_schedule_service

    def update_state(self, state: InputState) -> None:
        # Store przed managers update — listenery (logger, etc.) czytają
        # to po `_async_update_listeners`, więc state musi być świeży.
        self.last_input_state = state

        # BatteryScheduleService FIRST — may engage/disengage a slot, mutating
        # `schedule._currently_engaging` which flips `ems_interventions_blocked`.
        # Etap 0: service.update is no-op. Etap 2A wires real orchestration.
        self.battery_schedule_service.update(
            BatteryScheduleInput(battery_soc=state.battery_soc)
        )

        # Single read after service — source of truth for managers downstream.
        blocked = self.battery_schedule_repo.schedule.ems_interventions_blocked

        # grid_export PRZED water_heater — water_heater dostaje aktualny
        # `get_active_intervention()` (POSITIVE → reserved=3500W, NEGATIVE →
        # większy reserved by wymusić grzałki off).
        self.grid_export.update(state, ems_interventions_blocked=blocked)
        self.water_heater.update(
            state,
            self.grid_export.get_active_intervention(),
        )

        # DodPolicy maps phase + hysteresis + override → target_dod (numeric).
        # Owns _prev_block (hysteresis keep-state) — delegating phases call
        # block_pre_charge / block_post_charge / block_afternoon_dynamic
        # directly. DodPolicyActuator listens and writes to inverter via
        # scene.apply.
        self.dod_policy.update(state, ems_interventions_blocked=blocked)
        self._async_update_listeners()

    def update_rce(self, now: datetime, data: RcePrices) -> None:
        if not data:
            return
        self.rce_prices.update(now, data)
        self.charge_slots.update(data, self._heater_threshold())
        self.update_hourly(now)

    def update_hourly(self, now: datetime) -> None:
        self.rce_prices.update_hourly(now)
        self.charge_slots.rotate_if_day_changed(now)
        self.discharge_slots.update(self.rce_prices.rce_prices, now)
        if self.rce_prices.current_price is not None:
            self._async_update_listeners()

    def restore_rce_today(self, prices_attr: list[dict], now: datetime) -> None:
        """Restore today's RCE prices from sensor attributes."""
        self.rce_prices.restore_today(prices_attr, now)
        self.charge_slots.update(self.rce_prices.rce_prices, self._heater_threshold())
        self.update_hourly(now)

    def restore_rce_tomorrow(self, prices_attr: list[dict]) -> None:
        """Restore tomorrow's RCE prices from sensor attributes."""
        # Now nie jest istotne dla tomorrow restore (current_price czyta z today).
        # EmsRcePrices.restore_tomorrow przyjmuje now żeby utworzyć RcePrices
        # gdy first restore (rce_prices is None) — używamy datetime.now() jako
        # placeholder (nie wpływa na żadną logikę odczytową).
        self.rce_prices.restore_tomorrow(prices_attr, datetime.now())
        self.charge_slots.update(self.rce_prices.rce_prices, self._heater_threshold())

    def _heater_threshold(self) -> float:
        """Read input_number.heater_rce_threshold from last InputState (with fallback).

        Used by `charge_slots.update` callsites. None when state hasn't been
        received yet (early startup) → DEFAULT_HEATER_RCE_THRESHOLD.
        """
        if (
            self.last_input_state is not None
            and self.last_input_state.heater_rce_threshold is not None
        ):
            return self.last_input_state.heater_rce_threshold
        return DEFAULT_HEATER_RCE_THRESHOLD

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        def remove_listener() -> None:
            self._listeners.pop(remove_listener)

        self._listeners[remove_listener] = update_callback
        return remove_listener

    def _async_update_listeners(self) -> None:
        for update_callback in self._listeners.values():
            update_callback()
