"""Composition root: instancjonuje Ems application service + adapters.

Mała "wiring" warstwa łącząca application layer (`Ems` orchestrator) z
infrastructure (driven + driving adapters) dla HA. Zawiera tylko
`create_ems(hass, entry)` — factory wywoływana z `__init__.py:async_setup_entry`.

Layer responsibility (DDD):
- domain/ — pure aggregates (battery, grid_export, water_heater, rce,
  charge_slots, discharge_slots, ems_rce_prices)
- application/ — Ems orchestrator (composition + listeners + use cases)
- infrastructure/ — adapters (driven + driving)
- ems_factory.py — composition root, wiring all layers

Adaptery żyją w `infrastructure/`:
- `state_mapper.py` — HA states → InputState (driving adapter helpers)
- `dod_policy_repository.py` — DodPolicyRepository (driven: HA Storage)
- `dod_policy_logger.py` — DodPolicyLogger (driven: Python logging)
- `dod_policy_actuator.py` — DodPolicyActuator (driven: scene.apply)
- `goodwe_ems_actuator.py` — GoodweEmsActuator (driven: scene.apply, takes EmsOperation)
- `rce_api.py` — RceApi (driven: HTTP RCE prices)
"""

from datetime import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util.dt import now as now_local

from .application.battery_charge_service import BatteryChargeService
from .application.battery_schedule_service import BatteryScheduleService
from .application.ems import Ems
from .application.water_heater_reserved_service import WaterHeaterReservedService
from .domain.grid_export import GridExportManager
from .domain.input_state import InputState
from .domain.water_heater import WaterHeaterManager
from .infrastructure.async_task_runner import AsyncTaskRunner
from .infrastructure.battery_charge_current_actuator import BatteryChargeCurrentActuator
from .infrastructure.battery_charge_repository import BatteryChargeRepository
from .infrastructure.battery_schedule_repository import BatteryScheduleRepository
from .infrastructure.dod_policy_actuator import DodPolicyActuator
from .infrastructure.dod_policy_logger import DodPolicyLogger
from .infrastructure.dod_policy_repository import DodPolicyRepository
from .infrastructure.goodwe_ems_actuator import GoodweEmsActuator
from .infrastructure.state_mapper import listen_for_state_changes, update_input_state
from .infrastructure.water_heater_reserved_repository import (
    WaterHeaterReservedRepository,
)

_LOGGER = logging.getLogger(__name__)


async def create_ems(
    hass: HomeAssistant, entry: ConfigEntry, *, context_user_id: str
) -> Ems:
    """Composition root — wire domain (Ems) z driven + driving adapters."""
    # AsyncTaskRunner — shared by repo + service (and future adapters that
    # need to fire-and-forget tasks tied to this entry's lifecycle).
    tasks = AsyncTaskRunner(hass, entry)

    # Battery schedule: repo + service must exist BEFORE Ems (constructor deps).
    # Repo restore from .storage/ — defaults to fresh schedule if no persisted state.
    battery_schedule_repo = BatteryScheduleRepository(hass, tasks)
    await battery_schedule_repo.async_restore()
    battery_schedule_service = BatteryScheduleService(
        repo=battery_schedule_repo,
        clock=now_local,
        tasks=tasks,
    )

    # Battery charge: repo owns BatteryChargePolicy. Actuator depends on
    # repo. Service owns actuator (encapsulation of bounded context — Ems
    # only sees the Service). Etap B migration replaces
    # input_boolean.battery_charge_max_current_toggle.
    battery_charge_repo = BatteryChargeRepository(hass, tasks)
    await battery_charge_repo.async_restore()
    battery_charge_actuator = BatteryChargeCurrentActuator(
        hass, entry, battery_charge_repo, tasks, writes_enabled=True
    )
    battery_charge_service = BatteryChargeService(
        repo=battery_charge_repo,
        clock=now_local,
        actuator=battery_charge_actuator,
    )

    # Domain managers — owned by factory (used to be Ems-internal but moved
    # out so adapters can be constructor-injected without circular deps).
    grid_export = GridExportManager()
    water_heater = WaterHeaterManager()

    # DodPolicy is owned by DodPolicyRepository (parity with BatteryChargeRepository
    # + BatteryScheduleRepository pattern). Factory pulls the ref AFTER async_restore
    # so subsequent adapters (logger + actuator) get the persisted-state policy.
    # Repo restored BEFORE Ems construction so the first update_state tick sees
    # persisted target_dod (UNKNOWN-phase keep-state in DodPolicy.update preserves
    # it until inputs settle).
    dod_repository = DodPolicyRepository(hass, tasks)
    await dod_repository.async_restore()
    dod_policy = dod_repository.policy
    dod_logger = DodPolicyLogger(dod_policy)
    # Replaces YAML automation `ems-set-dod-from-block-discharge` (per ADR-019).
    dod_actuator = DodPolicyActuator(
        hass, dod_policy, tasks, context_user_id=context_user_id
    )
    goodwe_ems_actuator = GoodweEmsActuator(
        hass, tasks, context_user_id=context_user_id
    )

    # Water heater reserved-power policy — user-configurable via NumberEntity
    # (replaces hardcoded `reserved` in WaterHeaterManager._balanced_target).
    water_heater_reserved_repo = WaterHeaterReservedRepository(hass, tasks)
    await water_heater_reserved_repo.async_restore()
    water_heater_reserved_service = WaterHeaterReservedService(
        repo=water_heater_reserved_repo,
        clock=now_local,
    )

    ems: Ems = Ems(
        dod_policy=dod_policy,
        grid_export=grid_export,
        water_heater=water_heater,
        battery_schedule_service=battery_schedule_service,
        battery_charge_service=battery_charge_service,
        water_heater_reserved_service=water_heater_reserved_service,
        dod_repository=dod_repository,
        dod_logger=dod_logger,
        dod_actuator=dod_actuator,
        goodwe_ems_actuator=goodwe_ems_actuator,
    )

    @callback
    def update_hourly(now: datetime) -> None:
        ems.update_hourly(now)
        # Re-evaluate state — godzina ma znaczenie dla:
        # - battery.py: okien pre/post-charge
        # - grid_export.py: hour rollover defense (intervention zostaje
        #   ograniczona do bieżącej godziny — utility_meter resetuje hourly
        #   na pełnej godzinie); time-dependent NEGATIVE entry threshold
        #   przesuwa się przy minucie 45 (-0.05 → 0)
        # nawet gdy żaden z entity w HASS_STATE_MAPPER się nie zmienił.
        input_state = update_input_state(hass, InputState())
        ems.update_state(input_state)

    entry.async_on_unload(
        async_track_time_change(hass, update_hourly, minute=0, second=0)
    )
    update_hourly(now_local())

    # Driving adapter: HA state_changed event listener.
    listen_for_state_changes(hass, entry, ems)

    return ems
