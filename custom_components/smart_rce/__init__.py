"""The Smart RCE component."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module, reload
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .application.ems import Ems
from .application.energy_balance_service import EnergyBalanceService
from .application.target_soc_matrix_service import TargetSocMatrixService
from .application.weather_table_service import WeatherTableService
from .coordinator import SmartRceDataUpdateCoordinator
from .domain.weather_forecast_history import WeatherForecastHistory
from .ems_factory import create_ems
from .infrastructure.pv_forecast.realized_pv_loader import RealizedPvLoader
from .infrastructure.rce_api import RceApi
from .infrastructure.weather_history_loader import WeatherHistoryLoader
from .infrastructure.weather_listener import WeatherForecastListener
from .pv_forecast_factory import create_energy_balance_service

_LOGGER = logging.getLogger(__name__)

# Key used by an earlier code revision that created an HA system user
# for Context attribution. Logbook actually reads names from describers,
# not the auth store — so the user record is orphan now. Kept here as
# a constant for the one-shot cleanup in `_cleanup_orphan_system_user`.
CONF_USER_ID = "smart_rce_user_id"

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]


@dataclass
class SmartRceData:
    """Smart Rce Data.

    `battery_schedule_service` + `battery_charge_service` are accessible via
    `ems.battery_schedule_service` / `ems.battery_charge_service` — Ems is
    their wiring point and exposes them publicly. Repositories stay
    encapsulated inside the services (Etap C cleanup).
    """

    ems: Ems
    rce_coordinator: SmartRceDataUpdateCoordinator
    weather_listener: WeatherForecastListener
    pv_forecast: EnergyBalanceService
    weather_forecast_history: WeatherForecastHistory
    weather_table_service: WeatherTableService
    target_soc_matrix_service: TargetSocMatrixService


type SmartRceConfigEntry = ConfigEntry[SmartRceData]


async def async_setup_entry(hass: HomeAssistant, entry: SmartRceConfigEntry) -> bool:
    """Set up Smart RCE as config entry."""
    _LOGGER.debug("async_setup_entry")

    await _cleanup_orphan_system_user(hass, entry)

    websession = async_get_clientsession(hass)
    rceApi = RceApi(websession)
    ems: Ems = await create_ems(hass, entry)
    rce_coordinator = SmartRceDataUpdateCoordinator(hass, rceApi, ems, entry)
    weather_listener = WeatherForecastListener(hass, entry)

    weather_forecast_history = WeatherForecastHistory()
    pv_forecast = await create_energy_balance_service(
        hass, entry, weather_listener, weather_forecast_history, ems, rce_coordinator
    )

    weather_history_loader = WeatherHistoryLoader(hass)
    weather_table_service = WeatherTableService(
        hass, weather_history_loader, weather_listener
    )

    realized_pv_loader = RealizedPvLoader(hass)
    target_soc_matrix_service = TargetSocMatrixService(
        hass, pv_forecast, realized_pv_loader
    )

    await rce_coordinator.async_config_entry_first_refresh()

    entry.runtime_data = SmartRceData(
        ems,
        rce_coordinator,
        weather_listener,
        pv_forecast,
        weather_forecast_history,
        weather_table_service,
        target_soc_matrix_service,
    )

    _register_services(hass, weather_table_service, target_soc_matrix_service)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def _cleanup_orphan_system_user(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the "Smart RCE" system user created by an earlier code version.

    Earlier commits created an auth user to pass `Context(user_id=...)` to
    scene.apply, assuming HA logbook needed the user record to render
    attribution. It does not — `logbook.processor.ContextAugmenter` reads
    `LOGBOOK_ENTRY_NAME` directly from our describer for `smart_rce_action`
    events. The system user was dead weight; remove it on first reload.

    Idempotent: drops the user_id from entry.data and deletes the orphan
    user record from .storage/auth. No-op if both were already cleaned.
    """
    stored_id = entry.data.get(CONF_USER_ID)
    if not stored_id:
        return
    user = await hass.auth.async_get_user(stored_id)
    if user is not None:
        await hass.auth.async_remove_user(user)
        _LOGGER.info("Removed orphan system user 'Smart RCE' (id=%s)", stored_id)
    new_data = {k: v for k, v in entry.data.items() if k != CONF_USER_ID}
    hass.config_entries.async_update_entry(entry, data=new_data)


def _register_services(
    hass: HomeAssistant,
    weather_table_service: WeatherTableService,
    target_soc_matrix_service: TargetSocMatrixService,
) -> None:
    """Register smart_rce response-only services (idempotent)."""
    from datetime import date as date_type

    import voluptuous as vol

    from homeassistant.core import ServiceCall, ServiceResponse, SupportsResponse
    from homeassistant.exceptions import ServiceValidationError

    schema = vol.Schema({vol.Required("date"): str})

    def _parse_date(raw: str) -> date_type:
        try:
            return date_type.fromisoformat(raw)
        except ValueError as err:
            raise ServiceValidationError(
                f"Invalid date '{raw}', expected ISO YYYY-MM-DD"
            ) from err

    if not hass.services.has_service("smart_rce", "get_weather_table"):

        async def _handle_weather_table(call: ServiceCall) -> ServiceResponse:
            return await weather_table_service.async_get_table(
                _parse_date(call.data["date"])
            )

        hass.services.async_register(
            "smart_rce",
            "get_weather_table",
            _handle_weather_table,
            schema=schema,
            supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service("smart_rce", "get_target_soc_matrix"):

        async def _handle_target_soc_matrix(call: ServiceCall) -> ServiceResponse:
            return await target_soc_matrix_service.async_get_matrix(
                _parse_date(call.data["date"])
            )

        hass.services.async_register(
            "smart_rce",
            "get_target_soc_matrix",
            _handle_target_soc_matrix,
            schema=schema,
            supports_response=SupportsResponse.ONLY,
        )


def live_reload():
    reload(import_module("custom_components.smart_rce.const"))
    reload(import_module("custom_components.smart_rce.infrastructure.rce_api"))
    reload(import_module("custom_components.smart_rce.infrastructure"))
    reload(import_module("custom_components.smart_rce.domain.rce"))
    reload(import_module("custom_components.smart_rce.domain.input_state"))
    reload(import_module("custom_components.smart_rce.domain.block_discharge"))
    # grid_export PRZED water_heater — water_heater importuje InterventionDirection
    # z grid_export. Reload water_heater pre-grid_export → top-level import
    # zwraca OLD class → cross-module `is`/`==` identity break (`is` fail bo
    # różny class object; `==` OK bo StrEnum value-based).
    # Order in package: intervention (Protocol/VOs base) → positive/negative
    # → manager → __init__ (re-exports).
    reload(import_module("custom_components.smart_rce.domain.grid_export.intervention"))
    reload(import_module("custom_components.smart_rce.domain.grid_export.positive"))
    reload(import_module("custom_components.smart_rce.domain.grid_export.negative"))
    reload(import_module("custom_components.smart_rce.domain.grid_export.manager"))
    reload(import_module("custom_components.smart_rce.domain.grid_export"))
    reload(import_module("custom_components.smart_rce.domain.water_heater"))
    reload(import_module("custom_components.smart_rce.domain.charge_slots"))
    reload(import_module("custom_components.smart_rce.domain.discharge_slots"))
    reload(import_module("custom_components.smart_rce.domain.ems_rce_prices"))
    reload(import_module("custom_components.smart_rce.domain.dod_policy"))
    # battery_schedule: domain BEFORE application service (service imports domain).
    reload(import_module("custom_components.smart_rce.domain.battery_schedule"))
    # battery_charge_policy imports BatteryOperation from battery_schedule —
    # reload AFTER battery_schedule.
    reload(import_module("custom_components.smart_rce.domain.battery_charge_policy"))
    reload(
        import_module("custom_components.smart_rce.domain.water_heater_reserved_policy")
    )
    reload(
        import_module("custom_components.smart_rce.infrastructure.async_task_runner")
    )
    # apply_guard BEFORE actuators (dod_policy_actuator + goodwe_ems_actuator
    # import ApplyGuard from it).
    reload(import_module("custom_components.smart_rce.infrastructure.apply_guard"))
    # context_provider BEFORE actuators (both import fire_action_event_for_ctx).
    reload(import_module("custom_components.smart_rce.infrastructure.context_provider"))
    # repository base BEFORE concrete repositories (they extend Repository[T]).
    reload(import_module("custom_components.smart_rce.infrastructure.repository"))
    reload(
        import_module(
            "custom_components.smart_rce.infrastructure.battery_schedule_repository"
        )
    )
    reload(
        import_module(
            "custom_components.smart_rce.infrastructure.battery_charge_repository"
        )
    )
    reload(
        import_module(
            "custom_components.smart_rce.infrastructure.water_heater_reserved_repository"
        )
    )
    # battery_schedule_notifier BEFORE battery_schedule_service (service
    # constructor takes notifier as optional kwarg).
    reload(
        import_module(
            "custom_components.smart_rce.infrastructure.battery_schedule_notifier"
        )
    )
    # service base BEFORE concrete services (they extend Service[TRepo]).
    reload(import_module("custom_components.smart_rce.application.service"))
    reload(
        import_module(
            "custom_components.smart_rce.application.battery_schedule_service"
        )
    )
    reload(
        import_module("custom_components.smart_rce.application.battery_charge_service")
    )
    reload(
        import_module(
            "custom_components.smart_rce.application.water_heater_reserved_service"
        )
    )
    reload(import_module("custom_components.smart_rce.application.ems"))
    reload(import_module("custom_components.smart_rce.application"))
    reload(import_module("custom_components.smart_rce.domain"))
    # infrastructure modules PRZED ems_factory — composition root importuje
    # wszystkie 3 driven adapters + state_mapper driving adapter.
    reload(import_module("custom_components.smart_rce.infrastructure.state_mapper"))
    reload(
        import_module(
            "custom_components.smart_rce.infrastructure.dod_policy_repository"
        )
    )
    reload(
        import_module("custom_components.smart_rce.infrastructure.dod_policy_logger")
    )
    reload(
        import_module("custom_components.smart_rce.infrastructure.dod_policy_actuator")
    )
    reload(
        import_module("custom_components.smart_rce.infrastructure.goodwe_ems_actuator")
    )
    reload(
        import_module(
            "custom_components.smart_rce.infrastructure.battery_charge_current_actuator"
        )
    )
    reload(import_module("custom_components.smart_rce.ems_factory"))
    reload(import_module("custom_components.smart_rce.domain.target_soc"))
    reload(import_module("custom_components.smart_rce.domain.pv_forecast"))
    reload(import_module("custom_components.smart_rce.domain.target_soc_matrix"))
    reload(
        import_module(
            "custom_components.smart_rce.infrastructure.workday_calendar_reader"
        )
    )
    reload(
        import_module("custom_components.smart_rce.domain.pv_forecast_extrapolation")
    )
    reload(import_module("custom_components.smart_rce.domain.weather_forecast_history"))
    reload(import_module("custom_components.smart_rce.domain.weather_multiplier"))
    reload(import_module("custom_components.smart_rce.domain.weather_table"))
    reload(import_module("custom_components.smart_rce.infrastructure.weather_listener"))
    reload(
        import_module(
            "custom_components.smart_rce.infrastructure.weather_history_loader"
        )
    )
    reload(
        import_module("custom_components.smart_rce.infrastructure.weather_diff_writer")
    )
    reload(
        import_module(
            "custom_components.smart_rce.infrastructure.pv_forecast.solcast_reader"
        )
    )
    reload(
        import_module(
            "custom_components.smart_rce.infrastructure.pv_forecast.consumption_profile_loader"
        )
    )
    reload(
        import_module(
            "custom_components.smart_rce.infrastructure.pv_forecast.live_rate_reader"
        )
    )
    reload(
        import_module(
            "custom_components.smart_rce.infrastructure.pv_forecast.realized_pv_loader"
        )
    )
    reload(import_module("custom_components.smart_rce.infrastructure.pv_forecast"))
    reload(
        import_module("custom_components.smart_rce.application.energy_balance_service")
    )
    reload(
        import_module("custom_components.smart_rce.application.weather_table_service")
    )
    reload(
        import_module(
            "custom_components.smart_rce.application.target_soc_matrix_service"
        )
    )
    reload(import_module("custom_components.smart_rce.pv_forecast_factory"))
    reload(import_module("custom_components.smart_rce.coordinator"))
    reload(import_module("custom_components.smart_rce.sensor._state_writer_mixin"))
    reload(import_module("custom_components.smart_rce.sensor.rce_sensor"))
    reload(import_module("custom_components.smart_rce.sensor.energy_balance_sensor"))
    reload(import_module("custom_components.smart_rce.sensor.weather_history_sensor"))
    reload(import_module("custom_components.smart_rce.sensor.weather_table_sensor"))
    reload(import_module("custom_components.smart_rce.sensor.target_soc_matrix_sensor"))
    reload(import_module("custom_components.smart_rce.sensor.ems_sensor"))
    reload(import_module("custom_components.smart_rce.sensor"))
    reload(import_module("custom_components.smart_rce.ems_device"))
    reload(import_module("custom_components.smart_rce.binary_sensor"))
    reload(import_module("custom_components.smart_rce.button"))
    reload(import_module("custom_components.smart_rce.switch"))
    reload(import_module("custom_components.smart_rce.select"))
    reload(import_module("custom_components.smart_rce.time"))
    reload(import_module("custom_components.smart_rce.number"))
    reload(import_module("custom_components.smart_rce"))


async def async_unload_entry(hass: HomeAssistant, entry: SmartRceConfigEntry) -> bool:
    """Unload a config entry."""
    live_reload()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
