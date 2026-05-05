"""Composition root: instancjonuje domain (Ems), podpina adapters.

Mała "wiring" warstwa łącząca domain (`Ems` aggregate root) z infrastructure
(driven + driving adapters) dla HA. Zawiera tylko `create_ems(hass, entry)`
— factory wywoływana z `__init__.py:async_setup_entry`.

Adaptery i mapping infrastructure żyją w `infrastructure/`:
- `state_mapper.py` — HA states → InputState (driving adapter helpers)
- `battery_persistence.py` — BatteryStatePersistence (driven: HA Storage)
- `battery_logger.py` — BatteryManagerLogger (driven: Python logging)
- `grid_export_actuator.py` — GridExportActuator (driven: scene.apply)
- `rce_api.py` — RceApi (driven: HTTP RCE prices)
"""

from datetime import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util.dt import now as now_local

from .domain.ems import Ems
from .domain.input_state import InputState
from .infrastructure.battery_logger import BatteryManagerLogger
from .infrastructure.battery_persistence import BatteryStatePersistence
from .infrastructure.grid_export_actuator import GridExportActuator
from .infrastructure.state_mapper import listen_for_state_changes, update_input_state

_LOGGER = logging.getLogger(__name__)


async def create_ems(hass: HomeAssistant, entry: ConfigEntry) -> Ems:
    """Composition root — wire domain (Ems) z driven + driving adapters."""
    ems: Ems = Ems()

    # Driven adapter: Battery state persistence (HA Storage).
    # Restore PRZED pierwszym update_state — chroni przed race condition po
    # HA restart (template binary_sensor ładuje się 25-50ms po smart_rce).
    battery_persistence = BatteryStatePersistence(hass, entry, ems.battery)
    await battery_persistence.async_restore()
    ems.async_add_listener(battery_persistence.save_if_changed)

    # Driven adapter: BatteryManager observability (Python logging).
    battery_logger = BatteryManagerLogger(ems.battery, ems)
    ems.async_add_listener(battery_logger.log_if_changed)

    # Driven adapter: Goodwe EMS via scene.apply (fire-and-forget).
    actuator = GridExportActuator(hass, entry, ems)
    ems.async_add_listener(actuator.apply_if_changed)

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
