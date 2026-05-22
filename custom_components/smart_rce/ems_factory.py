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
- `grid_export_actuator.py` — GridExportActuator (driven: scene.apply)
- `rce_api.py` — RceApi (driven: HTTP RCE prices)
"""

from datetime import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import now as now_local

from .application.battery_charge_service import BatteryChargeService
from .application.battery_schedule_service import BatteryScheduleService
from .application.ems import Ems
from .domain.input_state import InputState
from .infrastructure.async_task_runner import AsyncTaskRunner
from .infrastructure.battery_charge_current_actuator import BatteryChargeCurrentActuator
from .infrastructure.battery_charge_repository import (
    STORAGE_KEY as BATTERY_CHARGE_STORAGE_KEY,
    STORAGE_VERSION as BATTERY_CHARGE_STORAGE_VERSION,
    BatteryChargeRepository,
)
from .infrastructure.battery_schedule_repository import (
    STORAGE_KEY as BATTERY_SCHEDULE_STORAGE_KEY,
    STORAGE_VERSION as BATTERY_SCHEDULE_STORAGE_VERSION,
    BatteryScheduleRepository,
)
from .infrastructure.dod_policy_actuator import DodPolicyActuator
from .infrastructure.dod_policy_logger import DodPolicyLogger
from .infrastructure.dod_policy_repository import DodPolicyRepository
from .infrastructure.grid_export_actuator import GridExportActuator
from .infrastructure.state_mapper import listen_for_state_changes, update_input_state

_LOGGER = logging.getLogger(__name__)


async def create_ems(hass: HomeAssistant, entry: ConfigEntry) -> Ems:
    """Composition root — wire domain (Ems) z driven + driving adapters."""
    # AsyncTaskRunner — shared by repo + service (and future adapters that
    # need to fire-and-forget tasks tied to this entry's lifecycle).
    tasks = AsyncTaskRunner(hass, entry)

    # Battery schedule: repo + service must exist BEFORE Ems (constructor deps).
    # Repo restore from .storage/ — defaults to fresh schedule if no persisted state.
    battery_schedule_store: Store[dict] = Store(
        hass, BATTERY_SCHEDULE_STORAGE_VERSION, BATTERY_SCHEDULE_STORAGE_KEY
    )
    battery_schedule_repo = BatteryScheduleRepository(battery_schedule_store, tasks)
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
    battery_charge_store: Store[dict] = Store(
        hass, BATTERY_CHARGE_STORAGE_VERSION, BATTERY_CHARGE_STORAGE_KEY
    )
    battery_charge_repo = BatteryChargeRepository(battery_charge_store, tasks)
    await battery_charge_repo.async_restore()
    battery_charge_actuator = BatteryChargeCurrentActuator(
        hass, battery_charge_repo, tasks
    )
    # Register 5-min drift refresh + delayed startup reconcile (lifecycle tied to entry).
    battery_charge_actuator.schedule_periodic_refresh(entry)
    battery_charge_service = BatteryChargeService(
        repo=battery_charge_repo,
        clock=now_local,
        actuator=battery_charge_actuator,
    )

    ems: Ems = Ems(
        battery_schedule_repo=battery_schedule_repo,
        battery_schedule_service=battery_schedule_service,
        battery_charge_service=battery_charge_service,
    )

    # Driven adapters — instantiated here (they hold Ems reference), then
    # attached to Ems for explicit dispatch within update_state. No listener
    # registration — flow visible inline in Ems.update_state body.
    # DodPolicyRepository: restore PRZED pierwszym update_state (chroni przed
    # race condition po HA restart; UNKNOWN-phase keep-state w DodPolicy.update
    # preserves persisted target_dod until inputs settle).
    dod_repository = DodPolicyRepository(hass, ems.dod_policy, tasks)
    await dod_repository.async_restore()

    dod_logger = DodPolicyLogger(ems.dod_policy)
    # Replaces YAML automation `ems-set-dod-from-block-discharge` (per ADR-019).
    dod_actuator = DodPolicyActuator(hass, ems.dod_policy, tasks)
    grid_export_actuator = GridExportActuator(hass, ems.grid_export, tasks)

    ems.attach_driven_adapters(
        dod_repository=dod_repository,
        dod_logger=dod_logger,
        dod_actuator=dod_actuator,
        grid_export_actuator=grid_export_actuator,
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
