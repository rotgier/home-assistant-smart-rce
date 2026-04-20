"""Adapter from Hass to Domain."""

from collections.abc import Callable
from datetime import datetime, time
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import (
    CoreState,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.event import (
    Event,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.util.dt import now as now_local

from .domain.ems import Ems
from .domain.input_state import InputState

_LOGGER = logging.getLogger(__name__)


_UNAVAILABLE_WARNED: set[str] = set()


def map_on_off(entity: str, value: str) -> bool | None:
    match value:
        case "on":
            _UNAVAILABLE_WARNED.discard(entity)
            return True
        case "off":
            _UNAVAILABLE_WARNED.discard(entity)
            return False
        case "unavailable" | "unknown":
            if entity not in _UNAVAILABLE_WARNED:
                _LOGGER.warning("State %s is %s — treating as None", entity, value)
                _UNAVAILABLE_WARNED.add(entity)
            return None
        case _:
            _LOGGER.error("State %s being %s cannot be mapped to bool", entity, value)
            return None


def map_float(entity: str, value: str) -> float | None:
    if value in ("unavailable", "unknown"):
        if entity not in _UNAVAILABLE_WARNED:
            _LOGGER.warning("State %s is %s — treating as None", entity, value)
            _UNAVAILABLE_WARNED.add(entity)
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        _LOGGER.error("State %s being %s cannot be mapped to float", entity, value)
        return None


def set_water_heater_big_is_on(entity: str, i: InputState, state: str) -> None:
    i.water_heater_big_is_on = map_on_off(entity, state)


def set_water_heater_small_is_on(entity: str, i: InputState, state: str) -> None:
    i.water_heater_small_is_on = map_on_off(entity, state)


def set_battery_soc(entity: str, i: InputState, state: str) -> None:
    i.battery_soc = map_float(entity, state)


def set_battery_charge_limit(entity: str, i: InputState, state: str) -> None:
    i.battery_charge_limit = map_float(entity, state)


def set_battery_power_2_minutes(entity: str, i: InputState, state: str) -> None:
    i.battery_power_2_minutes = map_float(entity, state)


def set_consumption_minus_pv_2_minutes(entity: str, i: InputState, state: str) -> None:
    i.consumption_minus_pv_2_minutes = map_float(entity, state)


def set_consumption_minus_pv_5_minutes(entity: str, i: InputState, state: str) -> None:
    i.consumption_minus_pv_5_minutes = map_float(entity, state)


def set_exported_energy_hourly(entity: str, i: InputState, state: str) -> None:
    i.exported_energy_hourly = map_float(entity, state)


def set_heater_mode(entity: str, i: InputState, state: str) -> None:
    i.heater_mode = state


def set_depth_of_discharge(entity: str, i: InputState, state: str) -> None:
    i.depth_of_discharge = map_float(entity, state)


def set_battery_charge_toggle_on(entity: str, i: InputState, state: str) -> None:
    i.battery_charge_toggle_on = map_on_off(entity, state)


def set_ems_allow_discharge_override(entity: str, i: InputState, state: str) -> None:
    i.ems_allow_discharge_override = map_on_off(entity, state)


def set_start_charge_hour_override(entity: str, i: InputState, state: str) -> None:
    """input_datetime.rce_start_charge_hour_today_override state → time."""
    if state in (None, "", "unavailable", "unknown"):
        i.start_charge_hour_override = None
        return
    try:
        # HA input_datetime (has_time, has_date=false) string format: "HH:MM:SS"
        i.start_charge_hour_override = time.fromisoformat(state)
    except ValueError:
        _LOGGER.error("State %s=%s cannot be parsed as time", entity, state)
        i.start_charge_hour_override = None


HASS_STATE_MAPPER: dict[str, Callable[[InputState, str], None]] = {
    "switch.water_heater_big_relay": set_water_heater_big_is_on,
    "switch.water_heater_small_relay": set_water_heater_small_is_on,
    "sensor.battery_state_of_charge": set_battery_soc,
    "sensor.battery_charge_limit": set_battery_charge_limit,
    "sensor.battery_power_avg_2_minutes": set_battery_power_2_minutes,
    "sensor.house_consumption_minus_heaters_minus_pv_avg_2_minutes": set_consumption_minus_pv_2_minutes,
    "sensor.house_consumption_minus_heaters_minus_pv_avg_5_minutes": set_consumption_minus_pv_5_minutes,
    "sensor.total_export_import_hourly": set_exported_energy_hourly,
    "input_select.ems_water_heater_mode": set_heater_mode,
    "number.goodwe_depth_of_discharge_on_grid": set_depth_of_discharge,
    "input_boolean.battery_charge_max_current_toggle": set_battery_charge_toggle_on,
    "input_boolean.ems_allow_discharge_override": set_ems_allow_discharge_override,
    "input_datetime.rce_start_charge_hour_today_override": set_start_charge_hour_override,
}


def update_input_state(hass: HomeAssistant, input_state: InputState) -> InputState:
    for entity, setter in HASS_STATE_MAPPER.items():
        state_object: State = hass.states.get(entity)
        if state_object is None:
            _LOGGER.error("State %s is not present in state machine", entity)
        else:
            setter(entity, input_state, state_object.state)
    input_state.now = now_local()
    return input_state


def listen_for_state_changes(hass: HomeAssistant, entry: ConfigEntry, ems: Ems) -> None:
    @callback
    def state_changed(event: Event[EventStateChangedData]) -> None:
        input_state: InputState = InputState()
        input_state = update_input_state(hass, input_state)
        # TODO is it needed to fetch state from event if it is taken directly from hass.states ? :)
        # or the other way around ... does it make sense to update_input_state on each state_changed
        new_state = event.data["new_state"]
        new_state = new_state.state if new_state else None
        entity_id = event.data["entity_id"]
        HASS_STATE_MAPPER[entity_id](entity_id, input_state, new_state)
        ems.update_state(input_state)

    @callback
    def hass_started(_=Event) -> None:
        _LOGGER.debug("hass_started")
        entry.async_on_unload(
            async_track_state_change_event(
                hass,
                HASS_STATE_MAPPER.keys(),
                state_changed,
            )
        )
        input_state: InputState = InputState()
        input_state = update_input_state(hass, input_state)
        ems.update_state(input_state)

    if hass.state == CoreState.running:
        _LOGGER.debug("hass is already running")
        hass_started()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, hass_started)


BATTERY_CHARGE_TOGGLE = "input_boolean.battery_charge_max_current_toggle"
BLOCK_BATTERY_CHARGE_SENSOR = "binary_sensor.ems_block_battery_charge"


def listen_for_block_battery_charge(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Toggle battery charge based on ems_block_battery_charge sensor."""

    async def block_battery_charge_changed(
        event: Event[EventStateChangedData],
    ) -> None:
        new_state = event.data["new_state"]
        old_state = event.data.get("old_state")
        if new_state is None or new_state.state not in ("on", "off"):
            return
        if old_state is None or old_state.state not in ("on", "off"):
            return
        service = "turn_off" if new_state.state == "on" else "turn_on"
        try:
            await hass.services.async_call(
                "input_boolean",
                service,
                {ATTR_ENTITY_ID: BATTERY_CHARGE_TOGGLE},
                blocking=True,
            )
        except Exception:
            _LOGGER.exception(
                "Failed to call input_boolean.%s on %s",
                service,
                BATTERY_CHARGE_TOGGLE,
            )

    @callback
    def hass_started(_=Event) -> None:
        entry.async_on_unload(
            async_track_state_change_event(
                hass,
                [BLOCK_BATTERY_CHARGE_SENSOR],
                block_battery_charge_changed,
            )
        )

    if hass.state == CoreState.running:
        hass_started()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, hass_started)


def create_ems(hass: HomeAssistant, entry: ConfigEntry) -> Ems:
    ems: Ems = Ems()

    @callback
    def update_hourly(now: datetime) -> None:
        ems.update_hourly(now)
        # Przelicz guard — zmiana godziny może otworzyć/zamknąć okno GUARD_END_HOUR
        # nawet gdy żaden z entity w HASS_STATE_MAPPER się nie zmienił.
        input_state = update_input_state(hass, InputState())
        ems.update_state(input_state)

    entry.async_on_unload(
        async_track_time_change(hass, update_hourly, minute=0, second=0)
    )
    update_hourly(now_local())

    listen_for_state_changes(hass, entry, ems)
    listen_for_block_battery_charge(hass, entry)

    return ems
