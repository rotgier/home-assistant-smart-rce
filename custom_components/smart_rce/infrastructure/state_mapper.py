"""HA states → InputState mapping (driving adapter helpers).

Konwertuje raw HA entity states (string values z `hass.states.get(...).state`)
na typed pola w domain `InputState` dataclass. Zawiera też `listen_for_state_changes`
— driving adapter który podpina się pod HA event bus i triggeruje
`ems.update_state(...)` po każdej zmianie state'u monitorowanego entity.

Architektura:
- `map_on_off`, `map_float` — primitive parsery z handling unavailable/unknown
- `set_X(entity, input_state, state)` funkcje — per-field setters
- `HASS_STATE_MAPPER` — dict entity_id → setter (dispatch)
- `update_input_state(hass, input_state)` — czyta wszystkie monitored entities
  z `hass.states`, builds full InputState
- `listen_for_state_changes(hass, entry, ems)` — registers state_changed
  listener na monitored entities, triggers `ems.update_state` po każdej zmianie

Wcześniej żyło w `adapter.py` (~280 linii). Wynesione żeby `adapter.py`
zachował tylko composition root (instancjowanie domain + adapters).
"""

from collections.abc import Callable
from datetime import time
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
from homeassistant.helpers.event import Event, async_track_state_change_event
from homeassistant.util.dt import now as now_local

from ..application.ems import Ems
from ..domain.input_state import InputState

_LOGGER = logging.getLogger(__name__)

# Per-entity dedup — pierwszy unavailable/unknown loguje WARNING, kolejne
# się dopóki entity nie wróci do valid state'u. Reset gdy state znowu OK.
_UNAVAILABLE_WARNED: set[str] = set()


# --- primitive parsers --- #


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


# --- per-field setters --- #
#
# Explicit named funkcje (nie lambdy z setattr) — `i.field = ...` jest
# refactor-friendly: rename pola w InputState aktualizuje wszystkie settery
# automatycznie via IDE. Lambdas z `setattr(i, "field", ...)` traciłyby tę
# safety bo string literal pola nie wykrywa się przez find-references.


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


def set_water_heater_strategy(entity: str, i: InputState, state: str) -> None:
    i.water_heater_strategy = state


def set_rce_should_hold_for_peak(entity: str, i: InputState, state: str) -> None:
    i.rce_should_hold_for_peak = map_on_off(entity, state)


def set_is_workday(entity: str, i: InputState, state: str) -> None:
    i.is_workday = map_on_off(entity, state)


def set_start_charge_hour_override(entity: str, i: InputState, state: str) -> None:
    """Parse input_datetime.rce_start_charge_hour_today_override state → time."""
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


def set_heater_rce_threshold(entity: str, i: InputState, state: str) -> None:
    i.heater_rce_threshold = map_float(entity, state)


def set_dod_override(entity: str, i: InputState, state: str) -> None:
    i.dod_override = map_float(entity, state)


def set_is_workday_tomorrow(entity: str, i: InputState, state: str) -> None:
    i.is_workday_tomorrow = map_on_off(entity, state)


def set_rce_high_price_threshold_gross(entity: str, i: InputState, state: str) -> None:
    i.rce_high_price_threshold_gross = map_float(entity, state)


# --- dispatch table: entity_id → setter --- #

HASS_STATE_MAPPER: dict[str, Callable[[str, InputState, str], None]] = {
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
    # `input_boolean.battery_charge_max_current_toggle` REMOVED (Etap B) —
    # replaced by smart_rce-owned select `select.ems_battery_charge_allowed_override`
    # backed by `BatteryChargePolicy.user_override_mode`. Combined decision
    # (override + schedule) flows through `BatteryChargeService.charge_allowed`,
    # passed as kwarg to managers (no longer via InputState).
    # `input_boolean.ems_allow_discharge_override` REMOVED — replaced by
    # smart_rce-owned switch `switch.ems_interventions_blocked` backed by
    # `BatterySchedule.ems_interventions_blocked` (Etap 0).
    "input_datetime.rce_start_charge_hour_today_override": set_start_charge_hour_override,
    "input_select.ems_water_heater_strategy": set_water_heater_strategy,
    "binary_sensor.rce_should_hold_for_peak": set_rce_should_hold_for_peak,
    "binary_sensor.workday": set_is_workday,
    "sensor.pv_power": set_pv_power,
    "sensor.pv_power_avg_2_minutes": set_pv_power_avg_2_minutes,
    "select.goodwe_ems_mode": set_goodwe_ems_mode,
    "binary_sensor.ems_other_automation_active_this_hour": set_other_ems_automation_active_this_hour,
    "input_select.smart_rce_grid_export_strategy_mode": set_grid_export_strategy_mode,
    "input_number.heater_rce_threshold": set_heater_rce_threshold,
    "input_number.ems_dod_override": set_dod_override,
    "binary_sensor.workday_tomorrow": set_is_workday_tomorrow,
    "input_number.rce_high_price_threshold_gross": set_rce_high_price_threshold_gross,
}


# --- update + listener --- #


def update_input_state(hass: HomeAssistant, input_state: InputState) -> InputState:
    """Read all monitored HA entities and write to InputState fields.

    Wywoływane po każdym state_changed (full re-read żeby InputState było
    spójne) oraz przy hourly tick i hass_started.
    """
    for entity, setter in HASS_STATE_MAPPER.items():
        state_object: State = hass.states.get(entity)
        if state_object is None:
            _LOGGER.error("State %s is not present in state machine", entity)
        else:
            setter(entity, input_state, state_object.state)
    input_state.now = now_local()
    return input_state


def listen_for_state_changes(hass: HomeAssistant, entry: ConfigEntry, ems: Ems) -> None:
    """Register HA state_changed listener — driving adapter for ems.update_state.

    Każda zmiana state'u monitored entity:
    1. Fresh InputState() + full re-read 21 entities z hass.states (defensive)
    2. Override zmienionego entity z event.new_state (state machine commit
       poprzedza event dispatch, więc i tak ten sam value — defensive on
       wypadek gdyby HA core kiedyś zmienił semantykę)
    3. ems.update_state(input_state) — domain orchestration

    Wstrzymane do `EVENT_HOMEASSISTANT_STARTED` jeśli hass nie running
    przy setupie integracji (zapobiega race condition gdy template sensors
    jeszcze nie zostały zinicjowane).
    """

    @callback
    def state_changed(event: Event[EventStateChangedData]) -> None:
        # Defensive dual read: full re-read 21 entities z hass.states +
        # override zmienionego entity z event.new_state. Mikro-overhead
        # akceptowalny (~kilka μs per event), w zamian:
        # - Fresh InputState per call — zero shared mutable state risk
        # - Defense-in-depth przy nowym polu bez initial populate
        # - Brak akumulacji state'u w długo żyjącym ems.last_input_state
        # State machine commit poprzedza event dispatch, więc hass.states
        # już zawiera new_value — override z event jest defensive
        # (gdyby kiedyś stało się inaczej). Patrz research DR-4.
        input_state: InputState = InputState()
        input_state = update_input_state(hass, input_state)
        new_state = event.data["new_state"]
        new_state_value = new_state.state if new_state else None
        entity_id = event.data["entity_id"]
        HASS_STATE_MAPPER[entity_id](entity_id, input_state, new_state_value)
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
