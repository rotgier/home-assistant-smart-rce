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
    StartChargeTodayChanged,
)
from custom_components.smart_rce.domain.discharge_slots import DischargeSlots
from custom_components.smart_rce.domain.dod_policy import DodPolicy
from custom_components.smart_rce.domain.ems_rce_prices import EmsRcePrices
from custom_components.smart_rce.domain.grid_export import GridExportManager
from custom_components.smart_rce.domain.input_state import InputState
from custom_components.smart_rce.domain.rce import RcePrices
from custom_components.smart_rce.domain.water_heater import WaterHeaterManager

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
    from custom_components.smart_rce.infrastructure.grid_export_actuator import (
        GridExportActuator,
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
        # Driven adapters (narrow domain refs — no Ems back-reference).
        dod_repository: DodPolicyRepository,
        dod_logger: DodPolicyLogger,
        dod_actuator: DodPolicyActuator,
        grid_export_actuator: GridExportActuator,
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
        self._dod_repository = dod_repository
        self._dod_logger = dod_logger
        self._dod_actuator = dod_actuator
        self._grid_export_actuator = grid_export_actuator

    def update_state(self, state: InputState) -> None:
        self.last_input_state = state

        # ─── 1. BatteryScheduleService — may flip ems_interventions_blocked ───
        # Etap 2A wires real orchestration (compute_operation engaging/disengaging
        # slots). Returns the BatteryOperation for downstream consumers.
        schedule_op = self.battery_schedule_service.update(
            BatteryScheduleInput(battery_soc=state.battery_soc)
        )

        # Source of truth for downstream managers — read via service properties,
        # not via repo (Etap C side fix — Ems doesn't leak repo).
        blocked = self.battery_schedule_service.ems_interventions_blocked
        schedule_active_this_hour = (
            self.battery_schedule_service.schedule_active_this_hour
        )

        # ─── 2. BatteryChargeService — caches schedule_op for derived queries ───
        # Service.charge_allowed + target_modbus_value become consistent with
        # this tick's schedule_op. Single read after update is the source of
        # truth for grid_export + water_heater (passed as explicit kwarg,
        # analogous to ems_interventions_blocked). Actuator dispatched below
        # state-diffs target vs Modbus cache, only writes on delta.
        self.battery_charge_service.update(schedule_op)
        charge_allowed = self.battery_charge_service.charge_allowed
        # start_charge_hour_override now owned by BatteryChargePolicy (Etap B'-2);
        # passed as explicit kwarg to consumers (DodPolicy, GridExport) — same
        # pattern as charge_allowed / ems_interventions_blocked.
        start_charge_hour_override = (
            self.battery_charge_service.start_charge_hour_override
        )

        # ─── 3. GridExportManager + its actuator (Goodwe scene.apply) ───
        # grid_export PRZED water_heater — water_heater dostaje aktualny
        # `get_active_intervention()` (POSITIVE → reserved=3500W, NEGATIVE →
        # większy reserved by wymusić grzałki off).
        self.grid_export.update(
            state,
            ems_interventions_blocked=blocked,
            battery_charge_allowed=charge_allowed,
            ems_schedule_active_this_hour=schedule_active_this_hour,
            start_charge_hour_override=start_charge_hour_override,
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
        self.dod_policy.update(
            state,
            ems_interventions_blocked=blocked,
            start_charge_hour_override=start_charge_hour_override,
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
        self._maybe_sync_start_charge_hour(now, rotation_event)
        self.discharge_slots.update(self.rce_prices.rce_prices, now)
        if self.rce_prices.current_price is not None:
            self._async_update_listeners()

    def restore_rce_today(self, prices_attr: list[dict], now: datetime) -> None:
        """Restore today's RCE prices from sensor attributes."""
        self.rce_prices.restore_today(prices_attr, now)
        self._refresh_charge_slots(now, self.rce_prices.rce_prices)
        self.update_hourly(now)

    def restore_rce_tomorrow(self, prices_attr: list[dict]) -> None:
        """Restore tomorrow's RCE prices from sensor attributes."""
        # Now nie jest istotne dla tomorrow restore (current_price czyta z today).
        # EmsRcePrices.restore_tomorrow przyjmuje now żeby utworzyć RcePrices
        # gdy first restore (rce_prices is None) — używamy datetime.now() jako
        # placeholder (nie wpływa na żadną logikę odczytową).
        now_local_dt = datetime.now()
        self.rce_prices.restore_tomorrow(prices_attr, now_local_dt)
        self._refresh_charge_slots(now_local_dt, self.rce_prices.rce_prices)

    def _refresh_charge_slots(self, now: datetime, rce_data: RcePrices | None) -> None:
        """Recompute charge_slots from RCE + propagate today_start change event.

        Etap B'-2: replaces legacy YAML automation
        `copy-rce-start-charge-override-midnight` — sync RCE-computed
        today_start into BatteryChargePolicy.start_charge_hour_override.
        """
        event = self.charge_slots.update(rce_data, self._heater_threshold())
        self._maybe_sync_start_charge_hour(now, event)

    def _maybe_sync_start_charge_hour(
        self, now: datetime, event: StartChargeTodayChanged | None
    ) -> None:
        """Propagate event to BatteryChargePolicy with stickiness gate.

        Conditions to sync (any one):
        1. Bootstrap — policy.start_charge_hour_override is None (fresh install
           or first deploy)
        2. Midnight window — `0 <= now.hour < 6` (mirrors legacy YAML automation
           `copy-rce-start-charge-override-midnight` time condition)

        Outside these, user's manual override on `time.ems_battery_charge_start_hour_override`
        is preserved (sticky). At midnight rotation the override resets to
        the freshly-computed today_start.
        """
        if event is None:
            return
        if self.battery_charge_service is None:
            return
        policy_current = self.battery_charge_service.start_charge_hour_override
        if policy_current is None or 0 <= now.hour < 6:
            self.battery_charge_service.auto_sync_start_charge_hour_override(
                event.new_value
            )

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
