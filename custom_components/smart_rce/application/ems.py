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
  2. GridExportManager.update + GoodweEmsActuator.apply_if_changed
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
from custom_components.smart_rce.application.water_heater_reserved_service import (
    WaterHeaterReservedService,
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
from custom_components.smart_rce.domain.water_heater_reserved_policy import (
    WaterHeaterReservedInput,
)

if TYPE_CHECKING:
    from custom_components.smart_rce.infrastructure.dod_policy_actuator import (
        DodPolicyActuator,
    )
    from custom_components.smart_rce.infrastructure.dod_policy_logger import (
        DodPolicyLogger,
    )
    from custom_components.smart_rce.infrastructure.dod_policy_repository import (
        DodPolicyRepository,
    )
    from custom_components.smart_rce.infrastructure.goodwe_ems_actuator import (
        GoodweEmsActuator,
    )

type CALLBACK_TYPE = Callable[[], None]

_LOGGER = logging.getLogger(__name__)


class Ems:
    def __init__(
        self,
        *,
        # Domain managers — passed in (factory owns construction order).
        dod_policy: DodPolicy,
        grid_export: GridExportManager,
        water_heater: WaterHeaterManager,
        # Application services.
        battery_schedule_service: BatteryScheduleService,
        battery_charge_service: BatteryChargeService,
        water_heater_reserved_service: WaterHeaterReservedService,
        # Driven adapters (narrow domain refs — no Ems back-reference).
        dod_repository: DodPolicyRepository,
        dod_logger: DodPolicyLogger,
        dod_actuator: DodPolicyActuator,
        goodwe_ems_actuator: GoodweEmsActuator,
    ) -> None:
        # BatteryScheduleRepository is NOT held by Ems — accessed via
        # battery_schedule_service properties (ems_interventions_blocked,
        # schedule_active_this_hour). Etap C side fix: bounded context
        # internals don't leak past the application service.
        # BatteryChargeCurrentActuator similarly — owned by BatteryChargeService.
        self._listeners: dict[CALLBACK_TYPE, CALLBACK_TYPE] = {}
        self.last_input_state: InputState | None = None
        # Ems-internal RCE state (no driven adapter, no external owner).
        self.rce_prices: EmsRcePrices = EmsRcePrices()
        self.charge_slots: ChargeSlots = ChargeSlots()
        self.discharge_slots: DischargeSlots = DischargeSlots()
        # Domain managers + adapters — constructor-injected (single-phase
        # init; no more attach_driven_adapters since ea381d9 dropped Ems
        # back-reference from adapters).
        self.water_heater = water_heater
        self.grid_export = grid_export
        self.dod_policy = dod_policy
        self.battery_schedule_service = battery_schedule_service
        self.battery_charge_service = battery_charge_service
        self.water_heater_reserved_service = water_heater_reserved_service
        self._dod_repository = dod_repository
        self._dod_logger = dod_logger
        self._dod_actuator = dod_actuator
        self._goodwe_ems_actuator = goodwe_ems_actuator

    def update_state(self, state: InputState) -> None:
        self.last_input_state = state

        # ─── 1. BatteryScheduleService — atomic snapshot of schedule decisions ───
        schedule_result = self.battery_schedule_service.update(
            BatteryScheduleInput(battery_soc=state.battery_soc)
        )

        # ─── 2. BatteryChargeService — apply charge policy + atomic snapshot ───
        charge_result = self.battery_charge_service.update(schedule_result.operation)

        # ─── 3. GridExportManager + GoodweEmsActuator (Goodwe scene.apply) ───
        # grid_export PRZED water_heater — water_heater dostaje aktualny
        # `get_active_intervention()` (POSITIVE → reserved=3500W, NEGATIVE →
        # większy reserved by wymusić grzałki off).
        grid_op = self.grid_export.update(
            state,
            ems_interventions_blocked=schedule_result.ems_interventions_blocked,
            battery_charge_allowed=charge_result.charge_allowed,
            ems_schedule_active_this_hour=schedule_result.schedule_active_this_hour,
            start_charge_hour_override=charge_result.start_charge_hour_override,
        )
        # Etap F TODO: _resolve_ems_operation(schedule_op, grid_op) — schedule
        # slots override grid intervention. For now grid_op is the only source.
        self._goodwe_ems_actuator.apply_if_changed(grid_op)

        # ─── 4. WaterHeaterManager (no driven adapter — pure recommendation) ───
        # WaterHeaterReservedService computes reserved-power value per tick
        # from current collaborator state (RCE / PV forecast / weather);
        # passed as kwarg to keep WaterHeaterManager HASS-unaware.
        reserved_balanced_full = (
            self.water_heater_reserved_service.compute_current_value(
                WaterHeaterReservedInput(
                    rce_today=None,  # TODO: wire from self.rce_prices
                    pv_forecast_today=None,  # TODO: wire from pv_forecast_service
                    weather_summary=None,  # TODO: wire from weather_listener
                )
            )
        )
        self.water_heater.update(
            state,
            self.grid_export.get_active_intervention(),
            battery_charge_allowed=charge_result.charge_allowed,
            reserved_balanced_full=reserved_balanced_full,
        )

        # ─── 5. DodPolicy + persistence + logger + actuator ───
        # Order matters: save before apply (state persisted even if apply fails),
        # log after compute (debug log captures intent), actuator last
        # (Modbus write to inverter).
        self.dod_policy.update(
            state,
            ems_interventions_blocked=schedule_result.ems_interventions_blocked,
            start_charge_hour_override=charge_result.start_charge_hour_override,
        )
        self._dod_repository.save_if_changed()
        self._dod_logger.log_if_changed(state)
        self._dod_actuator.apply_if_changed()

        # ─── 6. External listeners (sensors subscribing to ems state) ───
        # Kept for HA consumers like binary_sensor + future sensors that
        # observe ems state through `async_add_listener`. Note: the
        # BatteryChargeCurrentActuator (Modbus write for charge_current) is
        # owned by BatteryChargeService and dispatched inside its update()
        # above — no Ems-level reference (encapsulation per bounded context).
        self._async_update_listeners()

    def update_rce(self, now: datetime, data: RcePrices) -> None:
        if not data:
            return
        self.rce_prices.update(now, data)
        self._refresh_charge_slots(now, data)
        self.update_hourly(now)

    def update_hourly(self, now: datetime) -> None:
        self.rce_prices.update_hourly(now)
        rotation_event = self.charge_slots.rotate_if_day_changed(now)
        self.battery_charge_service.handle_start_charge_today_changed(
            rotation_event, now
        )
        self.discharge_slots.update(self.rce_prices.rce_prices, now)
        if self.rce_prices.current_price is not None:
            self._async_update_listeners()

    def restore_rce_today(self, prices_attr: list[dict], now: datetime) -> None:
        """Restore today's RCE prices from sensor attributes."""
        self.rce_prices.restore_today(prices_attr, now)
        self._refresh_charge_slots(now, self.rce_prices.rce_prices)
        self.update_hourly(now)

    def restore_rce_tomorrow(self, prices_attr: list[dict], now: datetime) -> None:
        """Restore tomorrow's RCE prices from sensor attributes.

        `now` is required (caller passes `dt_util.now()`) so the auto-sync
        gate in BatteryChargeService.handle_start_charge_today_changed
        evaluates the midnight window against HA-aware local time. Ems
        stays HASS-unaware — caller injects the time source.
        """
        self.rce_prices.restore_tomorrow(prices_attr, now)
        self._refresh_charge_slots(now, self.rce_prices.rce_prices)

    def _refresh_charge_slots(self, now: datetime, rce_data: RcePrices | None) -> None:
        """Recompute charge_slots from RCE + propagate today_start change event.

        Etap B'-2: replaces legacy YAML automation
        `copy-rce-start-charge-override-midnight` — sync RCE-computed
        today_start into BatteryChargePolicy.start_charge_hour_override.
        BatteryChargeService owns the stickiness gate (this method just
        bridges the event from ChargeSlots).
        """
        event = self.charge_slots.update(rce_data, self._heater_threshold())
        self.battery_charge_service.handle_start_charge_today_changed(event, now)

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
