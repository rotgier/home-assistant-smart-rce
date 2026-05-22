"""Energy Management System — orchestrator + per-domain dispatch.

After Etap 0:
- Source of truth for `ems_interventions_blocked` lives in `BatterySchedule`
  (domain aggregate persisted via `BatteryScheduleRepository`), read at start
  of `update_state` and passed explicitly as keyword argument to
  `GridExportManager.update` and `DodPolicy.update`.
- All driven adapters (repositories, loggers, actuators) are now wired via
  the Ems constructor and dispatched explicitly within `update_state` body,
  not via the listener mechanism. Flow is fully visible in the IDE
  (Ctrl+Click `update_state` shows every step in order).

The `async_add_listener` mechanism is preserved for external HA consumers
(binary_sensor + future sensors that subscribe to ems state changes). Driven
adapters that smart_rce owns moved to explicit dispatch.

Dispatch order in `update_state` body — per-domain blocks (manager update +
its associated driven adapters immediately after):
  1. BatteryScheduleService.update (may flip ems_interventions_blocked)
  2. GridExportManager.update + GridExportActuator.apply_if_changed
  3. WaterHeaterManager.update (no driven adapter)
  4. DodPolicy.update + DodPolicyRepository.save_if_changed
     + DodPolicyLogger.log_if_changed + DodPolicyActuator.apply_if_changed
  5. _async_update_listeners() (sensors, external subscribers)
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging
from typing import TYPE_CHECKING

from custom_components.smart_rce.application.battery_charge_service import (
    BatteryChargeService,
)
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

if TYPE_CHECKING:
    from custom_components.smart_rce.infrastructure.battery_charge_current_actuator import (
        BatteryChargeCurrentActuator,
    )
    from custom_components.smart_rce.infrastructure.dod_policy_actuator import (
        DodPolicyActuator,
    )
    from custom_components.smart_rce.infrastructure.dod_policy_logger import (
        DodPolicyLogger,
    )
    from custom_components.smart_rce.infrastructure.dod_policy_repository import (
        DodPolicyRepository,
    )
    from custom_components.smart_rce.infrastructure.grid_export_actuator import (
        GridExportActuator,
    )
    from homeassistant.util import dt as dt_util  # noqa: F401 — type hint hook

type CALLBACK_TYPE = Callable[[], None]

_LOGGER = logging.getLogger(__name__)


class Ems:
    def __init__(
        self,
        battery_schedule_repo: BatteryScheduleRepository | None = None,
        battery_schedule_service: BatteryScheduleService | None = None,
        battery_charge_service: BatteryChargeService | None = None,
    ) -> None:
        # Defaults to None for unit-test convenience (tests instantiate `Ems()`
        # and exercise individual managers like `ems.water_heater.update(...)`
        # without going through `update_state`). Production wiring in
        # `ems_factory.create_ems` always passes both. Driven adapters
        # (dod_repository, dod_logger, dod_actuator, grid_export_actuator) are
        # attached post-construction via `attach_driven_adapters` because Ems
        # itself is a dependency of those adapters (cyclical wiring resolved
        # by ordering in factory: Ems first, then adapters, then attach).
        self._listeners: dict[CALLBACK_TYPE, CALLBACK_TYPE] = {}
        self.last_input_state: InputState | None = None
        self.rce_prices: EmsRcePrices = EmsRcePrices()
        self.charge_slots: ChargeSlots = ChargeSlots()
        self.discharge_slots: DischargeSlots = DischargeSlots()
        self.water_heater: WaterHeaterManager = WaterHeaterManager()
        self.grid_export: GridExportManager = GridExportManager()
        self.dod_policy: DodPolicy = DodPolicy()
        self.battery_schedule_repo = battery_schedule_repo
        self.battery_schedule_service = battery_schedule_service
        self.battery_charge_service = battery_charge_service
        # Driven adapters — attached post-construction by factory.
        self._dod_repository: DodPolicyRepository | None = None
        self._dod_logger: DodPolicyLogger | None = None
        self._dod_actuator: DodPolicyActuator | None = None
        self._grid_export_actuator: GridExportActuator | None = None
        self._battery_charge_actuator: BatteryChargeCurrentActuator | None = None

    def attach_driven_adapters(
        self,
        *,
        dod_repository: DodPolicyRepository,
        dod_logger: DodPolicyLogger,
        dod_actuator: DodPolicyActuator,
        grid_export_actuator: GridExportActuator,
        battery_charge_actuator: BatteryChargeCurrentActuator,
    ) -> None:
        """Wire driven adapters after construction (factory call).

        Adapters depend on `Ems` in their own constructors, so we can't pass
        them via Ems.__init__ (would be cyclical). Factory creates Ems,
        creates adapters (with Ems reference), then calls this to link them
        for explicit dispatch in `update_state`.
        """
        self._dod_repository = dod_repository
        self._dod_logger = dod_logger
        self._dod_actuator = dod_actuator
        self._grid_export_actuator = grid_export_actuator
        self._battery_charge_actuator = battery_charge_actuator

    def update_state(self, state: InputState) -> None:
        from homeassistant.util import dt as dt_util

        self.last_input_state = state

        # ─── 1. BatteryScheduleService — may flip ems_interventions_blocked ───
        # Etap 2A wires real orchestration (compute_operation engaging/disengaging
        # slots). Returns the BatteryOperation for downstream consumers.
        schedule_op = self.battery_schedule_service.update(
            BatteryScheduleInput(battery_soc=state.battery_soc)
        )

        # Single read after service — source of truth for downstream managers.
        blocked = self.battery_schedule_repo.schedule.ems_interventions_blocked

        # ─── 2. BatteryChargeService — caches schedule_op for derived queries ───
        # Service.charge_allowed + target_modbus_value become consistent with
        # this tick's schedule_op. Single read after update is the source of
        # truth for grid_export + water_heater (passed as explicit kwarg,
        # analogous to ems_interventions_blocked). Actuator dispatched below
        # state-diffs target vs Modbus cache, only writes on delta.
        self.battery_charge_service.update(schedule_op)
        charge_allowed = self.battery_charge_service.charge_allowed

        # ─── 3. GridExportManager + its actuator (Goodwe scene.apply) ───
        # grid_export PRZED water_heater — water_heater dostaje aktualny
        # `get_active_intervention()` (POSITIVE → reserved=3500W, NEGATIVE →
        # większy reserved by wymusić grzałki off).
        self.grid_export.update(
            state,
            ems_interventions_blocked=blocked,
            battery_charge_allowed=charge_allowed,
        )
        self._grid_export_actuator.apply_if_changed(state)

        # ─── 4. WaterHeaterManager (no driven adapter — pure recommendation) ───
        self.water_heater.update(
            state,
            self.grid_export.get_active_intervention(),
            battery_charge_allowed=charge_allowed,
        )

        # ─── 5. DodPolicy + persistence + logger + actuator ───
        # Order matters: save before apply (state persisted even if apply fails),
        # log after compute (debug log captures intent), actuator last
        # (Modbus write to inverter).
        self.dod_policy.update(state, ems_interventions_blocked=blocked)
        self._dod_repository.save_if_changed()
        self._dod_logger.log_if_changed(state)
        self._dod_actuator.apply_if_changed()

        # ─── 6. BatteryChargeCurrentActuator — Modbus write for charge_current ───
        # Separate Modbus register from EMS mode / power_limit (handled above
        # in grid_export_actuator). State-diff against cached Modbus readback;
        # if target == current, no write fires.
        self._battery_charge_actuator.apply_if_changed(schedule_op, dt_util.now())

        # ─── 7. External listeners (sensors subscribing to ems state) ───
        # Kept for HA consumers like binary_sensor + future sensors that
        # observe ems state through `async_add_listener`.
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
