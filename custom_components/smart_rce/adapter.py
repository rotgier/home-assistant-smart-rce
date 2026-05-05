"""Adapter from Hass to Domain."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time
import logging
from typing import Any, Final

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
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import now as now_local

from .domain.battery import BatteryManager
from .domain.ems import Ems
from .domain.input_state import InputState

_LOGGER = logging.getLogger(__name__)

# --- BatteryManager persistence (HA Storage helper) --- #
BATTERY_STORAGE_VERSION: Final[int] = 1
BATTERY_STORAGE_KEY: Final[str] = "smart_rce_battery_manager"


class BatteryStatePersistence:
    """Application service: persists BatteryManager state across HA restarts.

    Domain (`BatteryManager`) jest pure — eksponuje `snapshot()`/`restore(data)`.
    Tutaj trzymamy `Store`, dispatchujemy save jako entry-scoped foreground
    task (musi przeżyć shutdown — `entry.async_create_task` jest waited
    przez `async_block_till_done`, w przeciwieństwie do background_task).
    """

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, manager: BatteryManager
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._manager = manager
        self._store: Store = Store(hass, BATTERY_STORAGE_VERSION, BATTERY_STORAGE_KEY)
        self._last_snapshot: dict | None = None

    async def async_restore(self) -> None:
        """Wywołać RAZ przed pierwszym update() w async_setup_entry."""
        data = await self._store.async_load()
        if data:
            self._manager.restore(data)
        self._last_snapshot = self._manager.snapshot()

    @callback
    def save_if_changed(self) -> None:
        """Persist snapshot na disk gdy zmienił się od ostatniego zapisu.

        Wywoływane jako listener po każdym ems.update_state.
        """
        current = self._manager.snapshot()
        if current == self._last_snapshot:
            return
        self._last_snapshot = current
        self._entry.async_create_task(
            self._hass,
            self._store.async_save(current),
            name="smart_rce_battery_save",
        )


# --- BatteryManager observability (logging) --- #

# Min interval (sec) between full DEBUG snapshots gdy stan key fields
# (phase + block_discharge) się NIE zmienia. Zapobiega spamowaniu logów
# co tick gdy nic się nie dzieje.
BATTERY_LOG_THROTTLE_SEC: Final[int] = 60


@dataclass
class _BatteryLogThrottle:
    """Throttling state dla DEBUG snapshot — nie część domain."""

    last_snapshot_key: tuple | None = None
    last_log_ts: datetime | None = None


class BatteryManagerLogger:
    """Application service: log INFO transitions + throttled DEBUG snapshot.

    Wzorzec analog do `BatteryStatePersistence` (ADR-018) — domain stays
    pure (zero `_LOGGER` calls, zero throttling state), service trzyma
    throttling + previous snapshot, registered jako listener via
    `ems.async_add_listener(log_if_changed)`. Po każdym `ems.update_state`
    czyta `manager.diagnostic_snapshot(state)` i emituje logi gdy
    relevant fields się zmieniły.

    Emit logów:
    1. **None-present DEBUG** — gdy phase=="none-present" (skip update)
    2. **Restored INFO** (jednorazowo) — pierwszy log_if_changed po
       async_restore wykrywa restored state (gdy `prev is None`).
    3. **Block_discharge transition INFO** — gdy `block_discharge` flipuje
    4. **Hour-start reset DEBUG** — pre-charge specific
    5. **Throttled DEBUG snapshot** — pełny dump key fields, max raz na
       BATTERY_LOG_THROTTLE_SEC gdy phase/block_discharge nie zmieni
    """

    def __init__(self, manager: BatteryManager, ems: Ems) -> None:
        self._manager = manager
        self._ems = ems
        self._prev: dict[str, Any] | None = None
        self._throttle = _BatteryLogThrottle()
        self._restored_logged = False

    @callback
    def log_if_changed(self) -> None:
        """Read diagnostic_snapshot, emit logs po zmianach (registered as ems listener)."""
        state = self._ems.last_input_state
        if state is None:
            return

        curr = self._manager.diagnostic_snapshot(state)
        prev = self._prev
        self._prev = curr

        if curr["phase"] == "none-present":
            _LOGGER.debug(
                "BatteryManager.update skipped (none_present): " "exported=%s now=%s",
                curr["exported_energy_hourly"],
                curr["now"],
            )
            return

        # Pierwszy "real" snapshot po starcie — log restored state jeśli
        # block_discharge=True (był persisted z poprzedniej sesji).
        if not self._restored_logged:
            self._restored_logged = True
            _LOGGER.info(
                "BatteryManager restored: block_discharge=%s last_hour=%s",
                curr["block_discharge"],
                curr["last_hour_seen"],
            )

        # INFO transition gdy block_discharge się flipuje
        if prev is not None and prev["block_discharge"] != curr["block_discharge"]:
            _LOGGER.info(
                "BatteryManager: block_discharge %s → %s (reason: %s)",
                prev["block_discharge"],
                curr["block_discharge"],
                curr["phase"],
            )

        # DEBUG hour-start reset (pre-charge specific)
        if (
            curr["phase"] == "pre-charge"
            and prev is not None
            and prev["last_hour_seen"] != curr["last_hour_seen"]
            and curr["now"] is not None
        ):
            _LOGGER.debug(
                "BatteryManager[pre-charge]: hour-start reset (hour=%d) "
                "→ block_discharge=False",
                curr["now"].hour,
            )

        self._maybe_log_snapshot(curr)

    def _maybe_log_snapshot(self, curr: dict[str, Any]) -> None:
        """Throttled DEBUG snapshot — log po zmianie phase/block_discharge lub timeout.

        Reduces log spam — pełny dump key fields max raz na
        BATTERY_LOG_THROTTLE_SEC gdy nic się nie zmienia.
        """
        snapshot_key = (curr["phase"], curr["block_discharge"])
        now = curr["now"]
        if now is None:
            return

        should_log = (
            self._throttle.last_snapshot_key is None
            or snapshot_key != self._throttle.last_snapshot_key
            or self._throttle.last_log_ts is None
            or (now - self._throttle.last_log_ts).total_seconds()
            >= BATTERY_LOG_THROTTLE_SEC
        )
        if not should_log:
            return

        exported = curr["exported_energy_hourly"]
        pv_avail_5m = curr["pv_available_5min"]
        _LOGGER.debug(
            "BatteryManager[%s] now=%s exported=%+.3fkWh(%+dWh) pv_avail_5m=%s "
            "DoD=%s toggle=%s charge_limit=%s override_window=%s | "
            "block_discharge=%s",
            curr["phase"],
            now.strftime("%H:%M:%S"),
            exported if exported is not None else 0.0,
            int(exported * 1000) if exported is not None else 0,
            f"{pv_avail_5m:+.0f}W" if pv_avail_5m is not None else "None",
            curr["depth_of_discharge"],
            curr["battery_charge_toggle_on"],
            curr["battery_charge_limit"],
            curr["start_charge_hour_override"],
            curr["block_discharge"],
        )
        self._throttle.last_snapshot_key = snapshot_key
        self._throttle.last_log_ts = now


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


class GridExportActuator:
    """Apply Goodwe EMS recommendations as fire-and-forget background tasks.

    Czyta `ems.grid_export.recommended_*` IN-MEMORY (bez round-trip przez
    output sensors). Rejestrowany przez `ems.async_add_listener(apply_if_changed)`
    — odpala się po każdym `ems.update_state` (state_changed / update_hourly).

    Wzorzec:
    1. `@callback apply_if_changed` (sync) — spawn fire-and-forget background
       task. Brak dedup tutaj — task spawn jest tani (eager_start=True +
       uncontested asyncio.Lock.acquire = no yield, fast-path skip task
       registration w config_entries.py:1383-1388).
    2. `_dispatch` (async) — `async with lock` → re-read in-memory →
       dedup vs `_last_applied` → `scene.apply`.

    Lock daje:
    - **Modbus serialization** — żaden wire interleave między concurrent
      scene.apply calls (Goodwe lib może mieć per-connection lock, ale
      ordering z naszej perspektywy niezdefiniowany bez tego).
    - **Coalescing** — burst N event'ów spawnuje N tasków; lock + re-read
      zostawia 1 actual scene.apply (vs N bez locka).

    `entry.async_create_background_task` — task auto-cancels przy entry
    unload + shutdown stage 2. Modbus mid-write przerwany jest OK
    (hardware utrzyma prev state).
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, ems: Ems) -> None:
        self._hass = hass
        self._entry = entry
        self._ems = ems
        self._lock = asyncio.Lock()
        # (mode, xset) — ostatnio zaaplikowana para; None = nigdy.
        self._last_applied: tuple[str, int | None] | None = None

    @callback
    def apply_if_changed(self) -> None:
        """Spawn fire-and-forget background task (registered as ems listener)."""
        self._entry.async_create_background_task(
            self._hass,
            self._dispatch(),
            name="smart_rce_grid_export_apply",
        )

    async def _dispatch(self) -> None:
        async with self._lock:
            # Re-read in-memory INSIDE locka — między schedule a acquire
            # mogły dojść kolejne event'y; używamy najświeższych wartości.
            mode = self._ems.grid_export.recommended_ems_mode
            xset = self._ems.grid_export.recommended_xset
            target = (mode, xset)
            if target == self._last_applied:
                return  # coalesce: same as last apply
            if mode is None:
                return  # invalid, skip without caching
            self._last_applied = target
            await self._apply_scene(mode, xset)

    async def _apply_scene(self, mode: str, xset: int | None) -> None:
        # scene.apply wymaga state jako string (homeassistant/scene.py:58
        # `_convert_states` raises na non-string). number/reproduce_state.py:24
        # parsuje przez float(state.state).
        entities: dict[str, str] = {GOODWE_EMS_MODE_SELECT: mode}
        if xset is not None and xset >= 0:
            entities[GOODWE_EMS_POWER_LIMIT_NUMBER] = str(xset)
        try:
            await self._hass.services.async_call(
                "scene",
                "apply",
                {"entities": entities},
                blocking=True,
            )
            _LOGGER.info(
                "GridExportActuator applied mode=%s xset=%s",
                mode,
                xset,
            )
        except Exception:
            _LOGGER.exception(
                "Failed to apply grid export recommendation mode=%s xset=%s",
                mode,
                xset,
            )


async def create_ems(hass: HomeAssistant, entry: ConfigEntry) -> Ems:
    ems: Ems = Ems()

    # Restore persistent BatteryManager state PRZED pierwszym update_state —
    # chroni przed race condition po HA restart (template binary_sensor
    # ładuje się 25-50ms po smart_rce sensors).
    battery_persistence = BatteryStatePersistence(hass, entry, ems.battery)
    await battery_persistence.async_restore()
    ems.async_add_listener(battery_persistence.save_if_changed)

    # BatteryManager observability — INFO transitions + throttled DEBUG
    # snapshot. Domain (battery.py) jest pure bez `_LOGGER`; logger
    # czyta `diagnostic_snapshot()` po każdym ems.update_state.
    battery_logger = BatteryManagerLogger(ems.battery, ems)
    ems.async_add_listener(battery_logger.log_if_changed)

    # Aktuator Goodwe EMS — czyta `ems.grid_export.recommended_*` in-memory
    # po każdym update_state (state_changed / update_hourly), dispatcuje
    # `scene.apply` jako fire-and-forget background task.
    actuator = GridExportActuator(hass, entry, ems)
    ems.async_add_listener(actuator.apply_if_changed)

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

    return ems
