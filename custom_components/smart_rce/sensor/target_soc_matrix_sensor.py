"""SmartRceTargetSocMatrixSensor — backs the dashboard target-SOC matrix card.

Wraps `TargetSocMatrixService.async_get_matrix()` as a state-bearing
entity so a markdown card can iterate
`state_attr('sensor.rce_target_soc_matrix', 'matrix')` from Jinja.

Trigger sources (mirror the weather-table sensor pattern):

1. `WeatherForecastListener.async_add_listener(...)` — every wetteronline
   forecast change. Weather drives PV adjustment → matrix cells move.
2. `EnergyBalanceService.async_add_listener(...)` — every recalc of the
   TargetSocCatalog aggregate (Solcast updates, charge_slots shift, minute
   tick refreshing extrapolated variants).
3. `async_track_state_change_event` on `input_datetime.energy_chart_date`
   — the dashboard's date picker (today / tomorrow / past).
"""

from __future__ import annotations

from datetime import date, datetime
import logging
from typing import Any, Final

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from ..application.energy_balance_service import EnergyBalanceService
from ..application.target_soc_matrix_service import TargetSocMatrixService
from ..const import DOMAIN
from ..coordinator import SmartRceDataUpdateCoordinator
from ..infrastructure.weather_listener import WeatherForecastListener

UNIQUE_ID: Final = f"{DOMAIN}_target_soc_matrix"
DATE_PICKER_ENTITY_ID: Final = "input_datetime.energy_chart_date"
# Toggle that flips between full-window and now-aware matrix semantics —
# changing it should trigger an immediate recompute, otherwise the user
# waits up to a minute for the next EnergyBalanceService tick.
NOW_AWARE_TOGGLE_ENTITY_ID: Final = "input_boolean.rce_target_soc_matrix_now_aware"

_LOGGER = logging.getLogger(__name__)


class SmartRceTargetSocMatrixSensor(SensorEntity):
    """Sensor whose `matrix` attribute is the full PV × Cons strategy table."""

    _attr_has_entity_name = True
    _attr_name = "Target SOC Matrix"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        service: TargetSocMatrixService,
        weather_listener: WeatherForecastListener,
        energy_balance_service: EnergyBalanceService,
        rce_coordinator: SmartRceDataUpdateCoordinator,
    ) -> None:
        self._hass = hass
        self._service = service
        self._weather_listener = weather_listener
        self._energy_balance_service = energy_balance_service
        self._attr_unique_id = UNIQUE_ID
        self._attr_device_info = rce_coordinator.device_info
        self._date: str | None = None
        self._kind: str | None = None
        self._matrix: dict[str, Any] | None = None
        self._last_compute_at: str | None = None

    @property
    def native_value(self) -> str | None:
        return self._last_compute_at

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "date": self._date,
            "kind": self._kind,
            "matrix": self._matrix,
            "last_compute_at": self._last_compute_at,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        remove_weather = self._weather_listener.async_add_listener(self._on_change)
        setattr(remove_weather, "_hass_callback", True)
        self.async_on_remove(remove_weather)

        remove_pv = self._energy_balance_service.async_add_listener(self._on_change)
        setattr(remove_pv, "_hass_callback", True)
        self.async_on_remove(remove_pv)

        self.async_on_remove(
            async_track_state_change_event(
                self._hass,
                [DATE_PICKER_ENTITY_ID, NOW_AWARE_TOGGLE_ENTITY_ID],
                self._on_date_change,
            )
        )

        self._schedule_recompute()
        _LOGGER.debug(
            "Setup of Target SOC Matrix sensor %s (unique_id: %s)",
            self.entity_id,
            self._attr_unique_id,
        )

    @callback
    def _on_change(self) -> None:
        self._schedule_recompute()

    @callback
    def _on_date_change(self, _event: Event[EventStateChangedData]) -> None:
        self._schedule_recompute()

    @callback
    def _schedule_recompute(self) -> None:
        self._hass.async_create_task(self._recompute())

    async def _recompute(self) -> None:
        target_date = self._read_target_date()
        try:
            result = await self._service.async_get_matrix(target_date)
        except Exception:  # noqa: BLE001 — defensive, never crash the entity
            _LOGGER.exception("TargetSocMatrix recompute failed for %s", target_date)
            return
        self._date = result["date"]
        self._kind = result["kind"]
        self._matrix = result["matrix"]
        self._last_compute_at = dt_util.now().isoformat()
        self.async_write_ha_state()

    def _read_target_date(self) -> date:
        state = self._hass.states.get(DATE_PICKER_ENTITY_ID)
        if state and state.state not in ("unknown", "unavailable", ""):
            try:
                return datetime.fromisoformat(state.state).date()
            except ValueError:
                pass
        return dt_util.now().date()
