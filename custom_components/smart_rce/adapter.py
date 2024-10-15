"""Adapter from Hass to Domain."""

from collections.abc import Callable
from datetime import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
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

from .domain.ems import Ems, InputState

_LOGGER = logging.getLogger(__name__)


def map_on_off(entity: str, value: str) -> bool | None:
    match value:
        case "on":
            return True
        case "off":
            return False
        case _:
            _LOGGER.error("State %s being %s cannot be mapped to bool", entity, value)
            return None


def map_float(entity: str, value: str) -> bool | None:
    try:
        return float(value)
    except (ValueError, TypeError):
        _LOGGER.error("State %s being %s cannot be mapped to float", entity, value)
        return None


def set_water_heater_is_on(entity: str, i: InputState, state: str) -> None:
    i.water_heater_is_on = map_on_off(entity, state)


def set_battery_soc(entity: str, i: InputState, state: str) -> None:
    i.battery_soc = map_float(entity, state)


def set_battery_power_2_minutes(entity: str, i: InputState, state: str) -> None:
    i.battery_power_2_minutes = map_float(entity, state)


def set_consumption_minus_pv_2_minutes(entity: str, i: InputState, state: str) -> None:
    i.consumption_minus_pv_2_minutes = map_float(entity, state)


def set_exported_energy_hourly(entity: str, i: InputState, state: str) -> None:
    i.exported_energy_hourly = map_float(entity, state)


HASS_STATE_MAPPER: dict[str, Callable[[InputState, str], None]] = {
    "switch.grzalka_wody_local": set_water_heater_is_on,
    "sensor.battery_state_of_charge": set_battery_soc,
    "sensor.battery_power_avg_2_minutes": set_battery_power_2_minutes,
    "sensor.house_consumption_minus_pv_avg_2_minutes": set_consumption_minus_pv_2_minutes,
    "sensor.total_export_import_hourly": set_exported_energy_hourly,
}


def update_input_state(hass: HomeAssistant, input_state: InputState) -> InputState:
    for entity, setter in HASS_STATE_MAPPER.items():
        state_object: State = hass.states.get(entity)
        if state_object is None:
            _LOGGER.error("State %s is not present in state machine", entity)
        else:
            setter(entity, input_state, state_object.state)
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


def create_ems(hass: HomeAssistant, entry: ConfigEntry) -> Ems:
    ems: Ems = Ems()

    @callback
    def update_hourly(now: datetime) -> None:
        ems.update_hourly(now)

    entry.async_on_unload(
        async_track_time_change(hass, update_hourly, minute=0, second=0)
    )
    update_hourly(now_local())

    listen_for_state_changes(hass, entry, ems)

    return ems
