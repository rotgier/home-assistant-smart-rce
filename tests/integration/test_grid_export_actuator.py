"""Integration tests for GridExportActuator (DR-3).

Wzorzec accuweather — pełny `hass` setup, real Ems + real listener wiring,
mock'owany jest jedynie HTTP boundary (RCE API) i `scene.apply` service
handler (zamiast goodwe custom_component setup).

Plan w `home-assistant-ops/research/2026-05-04-grid-export-actuator-test-plan.md`.
"""

from datetime import datetime
import logging
from unittest.mock import AsyncMock

from custom_components.smart_rce.domain.rce import TIMEZONE
from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import HomeAssistant, ServiceCall

from . import init_integration

# Bazowy stan dający POSITIVE entry: PV surplus 3000W, bateria niepełna,
# toggle on, hourly export 0.10 kWh > 0.06 BALANCE_GATE.
POSITIVE_INPUTS: dict[str, str] = {
    "sensor.battery_state_of_charge": "55.0",
    "sensor.battery_charge_limit": "18.0",
    "input_boolean.battery_charge_max_current_toggle": "on",
    "sensor.house_consumption_minus_heaters_minus_pv_avg_2_minutes": "-3000",
    "sensor.pv_power": "5000",
    "sensor.pv_power_avg_2_minutes": "5000",
    "sensor.total_export_import_hourly": "0.10",
    "binary_sensor.workday": "on",
    "input_datetime.rce_start_charge_hour_today_override": "08:00:00",
}


# Bazowy stan dający NEGATIVE entry: PV niskie 500W, bateria 85%, DoD=20
# (SoC > 100-DoD=80, discharge feasible), hourly -0.10 < -0.05 threshold.
NEGATIVE_INPUTS: dict[str, str] = {
    "sensor.battery_state_of_charge": "85.0",
    "sensor.battery_charge_limit": "18.0",
    "number.goodwe_depth_of_discharge_on_grid": "20.0",
    "sensor.house_consumption_minus_heaters_minus_pv_avg_2_minutes": "-500",
    "sensor.pv_power": "500",
    "sensor.pv_power_avg_2_minutes": "500",
    "sensor.total_export_import_hourly": "-0.10",
    "input_datetime.rce_start_charge_hour_today_override": "08:00:00",
}


@pytest.fixture
def at_eleven_oclock(freezer: FrozenDateTimeFactory):
    """11:00 — post-charge window (po start_charge_hour=08:00), not late hour."""
    freezer.move_to(datetime(2026, 5, 4, 11, 0, 0, tzinfo=TIMEZONE))
    return freezer


# --- Happy path ---


async def test_apply_on_positive_intervention_entry(
    hass: HomeAssistant,
    mock_rce_api: AsyncMock,
    mock_scene_apply: list[ServiceCall],
    set_smart_rce_inputs,
    at_eleven_oclock: FrozenDateTimeFactory,
) -> None:
    """Driving POSITIVE entry conditions → scene.apply z mode=charge_battery."""
    set_smart_rce_inputs()
    await init_integration(hass)
    mock_scene_apply.clear()

    set_smart_rce_inputs(POSITIVE_INPUTS)
    await hass.async_block_till_done()

    assert len(mock_scene_apply) >= 1, "Expected scene.apply after POSITIVE entry"
    entities = mock_scene_apply[-1].data["entities"]
    assert entities["select.goodwe_ems_mode"] == "charge_battery"
    assert int(entities["number.goodwe_ems_power_limit"]) > 0


async def test_apply_on_negative_intervention_entry(
    hass: HomeAssistant,
    mock_rce_api: AsyncMock,
    mock_scene_apply: list[ServiceCall],
    set_smart_rce_inputs,
    at_eleven_oclock: FrozenDateTimeFactory,
) -> None:
    """Driving NEGATIVE entry conditions → scene.apply z mode=discharge_battery."""
    set_smart_rce_inputs()
    await init_integration(hass)
    mock_scene_apply.clear()

    set_smart_rce_inputs(NEGATIVE_INPUTS)
    await hass.async_block_till_done()

    assert len(mock_scene_apply) >= 1, "Expected scene.apply after NEGATIVE entry"
    entities = mock_scene_apply[-1].data["entities"]
    # Bucket dla pv_avail=500 to (0, 1000, -1000) → DISCHARGE 1000W
    assert entities["select.goodwe_ems_mode"] == "discharge_battery"
    assert int(entities["number.goodwe_ems_power_limit"]) == 1000


async def test_apply_returning_to_auto_on_exit(
    hass: HomeAssistant,
    mock_rce_api: AsyncMock,
    mock_scene_apply: list[ServiceCall],
    set_smart_rce_inputs,
    at_eleven_oclock: FrozenDateTimeFactory,
) -> None:
    """Po POSITIVE intervention → soc=100 (ceiling) → exit → scene.apply mode=auto."""
    set_smart_rce_inputs()
    await init_integration(hass)

    # Wejdź w POSITIVE intervention
    set_smart_rce_inputs(POSITIVE_INPUTS)
    await hass.async_block_till_done()
    mock_scene_apply.clear()

    # Bateria pełna → exit (SOC_CEILING=100)
    set_smart_rce_inputs(POSITIVE_INPUTS | {"sensor.battery_state_of_charge": "100.0"})
    await hass.async_block_till_done()

    assert len(mock_scene_apply) >= 1, "Expected scene.apply on intervention exit"
    entities = mock_scene_apply[-1].data["entities"]
    assert entities["select.goodwe_ems_mode"] == "auto"
    # Auto mode — xset is None → number entity nieuwzględniony w scene.apply
    assert "number.goodwe_ems_power_limit" not in entities


# --- Dedup / coalescing ---


async def test_no_dispatch_when_recommended_unchanged(
    hass: HomeAssistant,
    mock_rce_api: AsyncMock,
    mock_scene_apply: list[ServiceCall],
    set_smart_rce_inputs,
    at_eleven_oclock: FrozenDateTimeFactory,
) -> None:
    """State change which doesn't shift recommended_* → 0 nowych scene.apply."""
    set_smart_rce_inputs()
    await init_integration(hass)
    await hass.async_block_till_done()
    mock_scene_apply.clear()

    # Drive irrelevant zmiany — pv_power, ale w neutral state (no intervention).
    # recommended_* zostaje (auto, None) → dedup vs _last_applied → no dispatch.
    set_smart_rce_inputs({"sensor.pv_power": "100"})
    await hass.async_block_till_done()
    set_smart_rce_inputs({"sensor.pv_power": "200"})
    await hass.async_block_till_done()

    assert (
        len(mock_scene_apply) == 0
    ), f"Expected 0 scene.apply, got {len(mock_scene_apply)}: {mock_scene_apply}"


async def test_burst_changes_coalesce_apply(
    hass: HomeAssistant,
    mock_rce_api: AsyncMock,
    mock_scene_apply: list[ServiceCall],
    set_smart_rce_inputs,
    at_eleven_oclock: FrozenDateTimeFactory,
) -> None:
    """Burst zmian recommended_* — lock+dedup koalescuje do final value.

    Pełne coalescing do 1 apply nie zawsze możliwe (multiple state_changed
    fire osobne callbacks, pierwszy task wystartuje przed kolejnymi
    state_changed). Ale: liczba scene.apply musi być < liczba event'ów
    (dedup działa) ORAZ ostatni apply odzwierciedla finalny stan.
    """
    # Wejdź już w POSITIVE intervention (xset_initial), by burst zmian był
    # tylko o pv_avail (1 entity per set), nie o entry conditions
    set_smart_rce_inputs(POSITIVE_INPUTS)
    await init_integration(hass)
    await hass.async_block_till_done()
    mock_scene_apply.clear()

    # Burst — 3 różne pv_avail bez block_till_done między. Każda zmiana =
    # 1 state_changed event (tylko consumption_minus_pv).
    set_smart_rce_inputs(
        POSITIVE_INPUTS
        | {"sensor.house_consumption_minus_heaters_minus_pv_avg_2_minutes": "-1500"}
    )
    set_smart_rce_inputs(
        POSITIVE_INPUTS
        | {"sensor.house_consumption_minus_heaters_minus_pv_avg_2_minutes": "-2500"}
    )
    set_smart_rce_inputs(
        POSITIVE_INPUTS
        | {"sensor.house_consumption_minus_heaters_minus_pv_avg_2_minutes": "-3500"}
    )
    await hass.async_block_till_done()

    # 3 events bez locka = 3+ apply (każdy z innym xset). Z lock+dedup
    # ostatni task widzi ten sam in-memory state co poprzedni → dedup.
    # Akceptujemy 1-3 apply (idealnie 1 jeśli wszystkie taski wejdą w lock
    # po pierwszym apply; gorszy case 3 jeśli każdy apply nadąży osobno).
    assert (
        1 <= len(mock_scene_apply) <= 3
    ), f"Expected 1-3 apply (coalesced from 3 events), got {len(mock_scene_apply)}"
    # Final state — pv_avail=3500W → bucket (3000, 4000) → xset=5000.
    final_entities = mock_scene_apply[-1].data["entities"]
    assert final_entities["select.goodwe_ems_mode"] == "charge_battery"
    assert int(final_entities["number.goodwe_ems_power_limit"]) == 5000


# --- Edge cases ---


async def test_skip_when_strategy_mode_disabled(
    hass: HomeAssistant,
    mock_rce_api: AsyncMock,
    mock_scene_apply: list[ServiceCall],
    set_smart_rce_inputs,
    at_eleven_oclock: FrozenDateTimeFactory,
) -> None:
    """strategy_mode=disabled → recommended zostaje auto, dedup → no spam."""
    set_smart_rce_inputs(
        {"input_select.smart_rce_grid_export_strategy_mode": "disabled"}
    )
    await init_integration(hass)
    await hass.async_block_till_done()
    mock_scene_apply.clear()

    # Drive POSITIVE-like inputs ale strategy disabled — manager forces auto.
    set_smart_rce_inputs(
        POSITIVE_INPUTS
        | {"input_select.smart_rce_grid_export_strategy_mode": "disabled"}
    )
    await hass.async_block_till_done()

    # Wszystkie apply z mode=auto (bo disabled override). Dedup po pierwszym → 0-1.
    for call in mock_scene_apply:
        assert call.data["entities"]["select.goodwe_ems_mode"] == "auto"


async def test_apply_logs_info_on_success(
    hass: HomeAssistant,
    mock_rce_api: AsyncMock,
    mock_scene_apply: list[ServiceCall],
    set_smart_rce_inputs,
    at_eleven_oclock: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Successful scene.apply → INFO log `GridExportActuator applied mode=...`."""
    set_smart_rce_inputs()
    await init_integration(hass)
    mock_scene_apply.clear()

    with caplog.at_level(logging.INFO, logger="custom_components.smart_rce.adapter"):
        set_smart_rce_inputs(POSITIVE_INPUTS)
        await hass.async_block_till_done()

    assert any(
        "GridExportActuator applied mode=charge_battery" in record.message
        for record in caplog.records
    ), f"Expected INFO log; got: {[r.message for r in caplog.records]}"


async def test_apply_logs_exception_on_service_failure(
    hass: HomeAssistant,
    mock_rce_api: AsyncMock,
    set_smart_rce_inputs,
    at_eleven_oclock: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """scene.apply raise → ERROR log z traceback, test nie pada."""

    async def failing_handler(call: ServiceCall) -> None:
        raise RuntimeError("simulated Modbus failure")

    hass.services.async_register("scene", "apply", failing_handler)

    set_smart_rce_inputs()
    await init_integration(hass)

    with caplog.at_level(logging.ERROR, logger="custom_components.smart_rce.adapter"):
        set_smart_rce_inputs(POSITIVE_INPUTS)
        await hass.async_block_till_done()

    assert any(
        "Failed to apply grid export recommendation" in record.message
        for record in caplog.records
    ), "Expected ERROR log on service failure"


# --- Lifecycle ---


async def test_actuator_unregistered_on_entry_unload(
    hass: HomeAssistant,
    mock_rce_api: AsyncMock,
    mock_scene_apply: list[ServiceCall],
    set_smart_rce_inputs,
    at_eleven_oclock: FrozenDateTimeFactory,
) -> None:
    """Po unload entry — kolejne state changes nie spawn'ują actuator task."""
    set_smart_rce_inputs()
    entry = await init_integration(hass)
    await hass.async_block_till_done()

    # Unload entry — listener powinien zostać wywyłączony przez async_on_unload
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    mock_scene_apply.clear()

    # Drive POSITIVE — bez integracji aktywnej, recommended_* nie istnieje
    # już, listener jest unregistered → 0 scene.apply
    set_smart_rce_inputs(POSITIVE_INPUTS)
    await hass.async_block_till_done()

    assert (
        len(mock_scene_apply) == 0
    ), f"Expected 0 scene.apply post-unload, got {len(mock_scene_apply)}"
