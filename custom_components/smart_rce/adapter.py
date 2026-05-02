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


def set_water_heater_strategy(entity: str, i: InputState, state: str) -> None:
    i.water_heater_strategy = state


def set_rce_should_hold_for_peak(entity: str, i: InputState, state: str) -> None:
    i.rce_should_hold_for_peak = map_on_off(entity, state)


def set_is_workday(entity: str, i: InputState, state: str) -> None:
    i.is_workday = map_on_off(entity, state)


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


def set_pv_power(entity: str, i: InputState, state: str) -> None:
    i.pv_power = map_float(entity, state)


def set_pv_power_avg_2_minutes(entity: str, i: InputState, state: str) -> None:
    i.pv_power_avg_2_minutes = map_float(entity, state)


def set_goodwe_ems_mode(entity: str, i: InputState, state: str) -> None:
    if state in (None, "", "unavailable", "unknown"):
        i.goodwe_ems_mode = None
        return
    i.goodwe_ems_mode = state


def set_other_ems_automation_active_this_hour(
    entity: str, i: InputState, state: str
) -> None:
    i.other_ems_automation_active_this_hour = map_on_off(entity, state)


def set_grid_export_strategy_mode(entity: str, i: InputState, state: str) -> None:
    if state in (None, "", "unavailable", "unknown"):
        i.grid_export_strategy_mode = None
        return
    i.grid_export_strategy_mode = state


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
    "input_select.ems_water_heater_strategy": set_water_heater_strategy,
    "binary_sensor.rce_should_hold_for_peak": set_rce_should_hold_for_peak,
    "binary_sensor.workday": set_is_workday,
    "sensor.pv_power": set_pv_power,
    "sensor.pv_power_avg_2_minutes": set_pv_power_avg_2_minutes,
    "select.goodwe_ems_mode": set_goodwe_ems_mode,
    "binary_sensor.ems_other_automation_active_this_hour": set_other_ems_automation_active_this_hour,
    "input_select.smart_rce_grid_export_strategy_mode": set_grid_export_strategy_mode,
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


GOODWE_EMS_MODE_SELECT = "select.goodwe_ems_mode"
GOODWE_EMS_POWER_LIMIT_NUMBER = "number.goodwe_ems_power_limit"
GRID_EXPORT_RECOMMENDED_MODE_SENSOR = "sensor.ems_grid_export_recommended_ems_mode"
GRID_EXPORT_RECOMMENDED_XSET_SENSOR = "sensor.ems_grid_export_recommended_xset"


def listen_for_grid_export_recommendations(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Apply Goodwe EMS mode/Xset based on smart_rce GridExportManager outputs.

    Nasłuchuje na zmiany sensorów `sensor.ems_grid_export_recommended_ems_mode`
    i `sensor.ems_grid_export_recommended_xset`. Gdy któryś się zmieni, ustawia
    odpowiednio `select.goodwe_ems_mode` i `number.goodwe_ems_power_limit`.

    Idempotent: śledzi `last_mode/last_xset` i pomija no-op calls.
    Order: xset first (jeśli applicable), potem mode — żeby nowy mode od razu
    używał aktualnego xset (xset jest ignorowany w trybach AUTO/STANDBY ale
    set_value nieszkodliwy).
    """
    last_mode: str | None = None
    last_xset: str | None = None  # raw state string ("None" lub liczba)

    async def _apply(mode: str, xset: str | None) -> None:
        try:
            # xset nie jest "None"/"unknown" → set_value
            if xset is not None and xset not in ("unknown", "unavailable"):
                try:
                    xset_int = int(float(xset))
                except (ValueError, TypeError):
                    xset_int = None
                if xset_int is not None and xset_int >= 0:
                    await hass.services.async_call(
                        "number",
                        "set_value",
                        {
                            ATTR_ENTITY_ID: GOODWE_EMS_POWER_LIMIT_NUMBER,
                            "value": xset_int,
                        },
                        blocking=True,
                    )
            await hass.services.async_call(
                "select",
                "select_option",
                {
                    ATTR_ENTITY_ID: GOODWE_EMS_MODE_SELECT,
                    "option": mode,
                },
                blocking=True,
            )
        except Exception:
            _LOGGER.exception(
                "Failed to apply grid export recommendation mode=%s xset=%s",
                mode,
                xset,
            )

    async def _on_change(event: Event[EventStateChangedData]) -> None:
        nonlocal last_mode, last_xset
        mode_state = hass.states.get(GRID_EXPORT_RECOMMENDED_MODE_SENSOR)
        xset_state = hass.states.get(GRID_EXPORT_RECOMMENDED_XSET_SENSOR)
        if mode_state is None or mode_state.state in ("unknown", "unavailable"):
            return
        mode = mode_state.state
        xset = xset_state.state if xset_state else None
        if mode == last_mode and xset == last_xset:
            return
        last_mode, last_xset = mode, xset
        await _apply(mode, xset)

    @callback
    def hass_started(_=Event) -> None:
        entry.async_on_unload(
            async_track_state_change_event(
                hass,
                [
                    GRID_EXPORT_RECOMMENDED_MODE_SENSOR,
                    GRID_EXPORT_RECOMMENDED_XSET_SENSOR,
                ],
                _on_change,
            )
        )

    if hass.state == CoreState.running:
        hass_started()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, hass_started)


async def create_ems(hass: HomeAssistant, entry: ConfigEntry) -> Ems:
    ems: Ems = Ems(hass=hass)

    # Restore persistent BatteryManager state PRZED pierwszym update_state —
    # chroni przed race condition po HA restart (template binary_sensor
    # ładuje się 25-50ms po smart_rce sensors).
    await ems.battery.async_restore()

    @callback
    def update_hourly(now: datetime) -> None:
        ems.update_hourly(now)
        # Przelicz state — godzina ma znaczenie dla:
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

    listen_for_state_changes(hass, entry, ems)
    listen_for_grid_export_recommendations(hass, entry)

    return ems
