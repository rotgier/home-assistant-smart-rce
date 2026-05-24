"""Common fixtures for Smart RCE integration tests."""

from collections.abc import Generator
from datetime import datetime
from unittest.mock import AsyncMock, patch

from custom_components.smart_rce.domain.rce import TIMEZONE, RceDayPrices
import pytest

from homeassistant.core import HomeAssistant, ServiceCall


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> None:
    """Auto-enable loading of custom_components dla każdego testu."""


@pytest.fixture(autouse=True)
async def hass_warsaw_timezone(hass: HomeAssistant) -> None:
    """Wymuszaj Europe/Warsaw timezone w hass — smart_rce logic depends on it.

    Bez tego `now_local()` zwraca czas w timezone runtime (UTC w CI, PDT
    lokalnie u devów), state.now.hour się rozchodzi z hardcoded oknami
    (pre-charge 7-8, afternoon 13-19, etc.).
    """
    await hass.config.async_set_time_zone("Europe/Warsaw")


@pytest.fixture(autouse=True)
def mock_weather_listener() -> Generator[None]:
    """Stub WeatherForecastListener — testy actuator nie potrzebują weather.

    Bez tego async_setup_entry pada na `KeyError: 'weather'` w
    `_register_for_weather_updates` (weather component nieładowany w testach).
    """
    with patch(
        "custom_components.smart_rce.infrastructure.weather_listener.WeatherForecastListener._register_for_weather_updates",
    ):
        yield


@pytest.fixture(autouse=True, scope="session")
def disable_live_reload() -> None:
    """No-op `live_reload()` na poziomie sesji.

    Smart_rce production reloaduje wszystkie moduły domain/adapter przy
    `async_unload_entry` (dev convenience po deploy). W testach to powoduje
    **identity break enumów** — `InterventionDirection.POSITIVE` przed reload
    != po reload (różne class objects mimo tej samej wartości). Bez tego
    no-op'a unit testy używające `is InterventionDirection.X` po integration
    teście padają.

    Session-scoped + setattr (nie patch context manager), żeby override
    przeżył teardown każdego pojedynczego testu. Order fixture teardown
    LIFO przy patch context manager mógł powodować że live_reload był
    unmockowany przed `async_unload_entry`.
    """
    import custom_components.smart_rce as smart_rce_module

    smart_rce_module.live_reload = lambda: None


@pytest.fixture(autouse=True)
def mock_pv_forecast_start() -> Generator[None]:
    """Stub PvForecastService.refresh_profiles_full — bez recorder query.

    pv_forecast_factory deferred initial fetch wywołuje
    service.refresh_profiles_full() po EVENT_HOMEASSISTANT_STARTED;
    bez stuba triggeruje recorder statistics_during_period (wymaga
    skomplikowanego setupu). Aktuator nie potrzebuje PV forecast → no-op stub.
    """
    with patch(
        "custom_components.smart_rce.application.pv_forecast_service.PvForecastService.refresh_profiles_full",
        new=AsyncMock(),
    ):
        yield


@pytest.fixture
def mock_rce_today_prices() -> list[float]:
    """Build default 24h RCE prices fixture (hourly PLN/MWh)."""
    return [
        500,
        480,
        460,
        450,
        440,
        430,  # 0-5 night
        420,
        280,
        250,
        200,
        150,
        100,  # 6-11 morning charge window
        80,
        70,
        60,
        50,
        80,
        200,  # 12-17 cheap PV peak
        450,
        600,
        700,
        650,
        580,
        520,  # 18-23 evening peak
    ]


@pytest.fixture
def mock_rce_api(mock_rce_today_prices: list[float]) -> Generator[AsyncMock]:
    """Mock RceApi.async_get_prices — bez HTTP do api.raporty.pse.pl."""

    def _build_day_prices(day: datetime) -> RceDayPrices:
        return RceDayPrices(
            published_at=datetime(day.year, day.month, day.day, 14, 0, tzinfo=TIMEZONE),
            day=day.date(),
            hour_price=tuple(mock_rce_today_prices),
        )

    async def _fetch(day: datetime) -> RceDayPrices:
        return _build_day_prices(day)

    with patch(
        "custom_components.smart_rce.infrastructure.rce_api.RceApi.async_get_prices",
        new=AsyncMock(side_effect=_fetch),
    ) as mock:
        yield mock


@pytest.fixture
async def mock_scene_apply(hass: HomeAssistant) -> list[ServiceCall]:
    """Capture scene.apply service calls + propagate states (for read-back actuators).

    Zamiast mockowania goodwe custom_component (overkill — wymagałoby
    inverter library mock + USB serial), interceptujemy `scene.apply`
    i record'ujemy wszystkie invocations. Testy asercyjne na contenct
    `entities` dict.

    Also propagate the written entity state (DodPolicyActuator reads back
    `number.goodwe_depth_of_discharge_on_grid` after each apply to verify
    propagation; without state update mock would always trigger silent_fail).
    """
    captured: list[ServiceCall] = []

    async def handler(call: ServiceCall) -> None:
        captured.append(call)
        # NOTE: We deliberately don't propagate state to entities via
        # async_set — it would trigger smart_rce state listener feedback
        # loop (DoD apply → state_changed → ems.update_state → another
        # apply tick). Tests using DodPolicyActuator may see silent_fail
        # logs (post_write read returns stale state); script.turn_on no-op
        # below absorbs the notify_alert call without exception.

    hass.services.async_register("scene", "apply", handler)

    # script.turn_on no-op — DodPolicyActuator calls notify_alert on silent_fail.
    async def script_handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("script", "turn_on", script_handler)
    return captured


def grid_export_scene_calls(scene_calls: list[ServiceCall]) -> list[ServiceCall]:
    """Filter scene.apply calls to grid_export-related ones.

    DodPolicyActuator also uses scene.apply (writes DoD register). Tests for
    GoodweEmsActuator should filter to calls touching `select.goodwe_ems_mode`.
    """
    return [
        c for c in scene_calls if "select.goodwe_ems_mode" in c.data.get("entities", {})
    ]


# Pełna lista smart_rce inputs z HASS_STATE_MAPPER (adapter.py:164-186).
# Defaults dobrane tak żeby smart_rce mógł wystartować bez błędów —
# valid floats / on/off / select options.
SMART_RCE_DEFAULTS: dict[str, str] = {
    "switch.water_heater_big_relay": "off",
    "switch.water_heater_small_relay": "off",
    "sensor.battery_state_of_charge": "50.0",
    "sensor.battery_charge_limit": "0.0",
    "sensor.battery_power_avg_2_minutes": "0.0",
    "sensor.house_consumption_minus_heaters_minus_pv_avg_2_minutes": "0.0",
    "sensor.house_consumption_minus_heaters_minus_pv_avg_5_minutes": "0.0",
    "sensor.total_export_import_hourly": "0.0",
    "input_select.ems_water_heater_mode": "BALANCED",
    "number.goodwe_depth_of_discharge_on_grid": "20.0",
    "input_boolean.battery_charge_max_current_toggle": "off",
    "input_boolean.ems_interventions_blocked": "off",
    "input_datetime.rce_start_charge_hour_today_override": "08:00:00",
    "input_select.ems_water_heater_strategy": "BATTERY_FIRST",
    "binary_sensor.rce_should_hold_for_peak": "off",
    "binary_sensor.workday": "on",
    "sensor.pv_power": "0.0",
    "sensor.pv_power_avg_2_minutes": "0.0",
    "select.goodwe_ems_mode": "auto",
    "binary_sensor.ems_other_automation_active_this_hour": "off",
    "input_select.smart_rce_grid_export_strategy_mode": "charge_adaptive",
}


@pytest.fixture
def set_smart_rce_inputs(hass: HomeAssistant):
    """Set defaults + overrides dla wszystkich smart_rce inputs.

    Argument `overrides` to dict (entity_id mają kropki, więc nie kwargs):
        set_smart_rce_inputs({"sensor.battery_state_of_charge": "55.0"})

    Smart_rce listenuje 21 input entities przez `async_track_state_change_event`.
    Setup MUSI być wywołany PRZED `init_integration(hass)` żeby
    `update_input_state` znalazł stany.
    """

    def _set(overrides: dict[str, str] | None = None) -> None:
        for entity_id, value in {**SMART_RCE_DEFAULTS, **(overrides or {})}.items():
            hass.states.async_set(entity_id, value)

    return _set
