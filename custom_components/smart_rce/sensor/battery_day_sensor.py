"""The picked day's battery bands for the price chart (`sensor.ems_battery_day`).

Follows the dashboard's date picker (`input_datetime.energy_chart_date`).
State: where the day comes from — "live" (today, tomorrow), "recorder" (a
past day) or "none". Attributes: sessions, upcoming runs, evening slots and the
PV charge bands of that day, ready for the chart; kept out of the recorder.
A live day is refreshed with every PV charge minute.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import MATCH_ALL
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..ems_device import ems_device_info

if TYPE_CHECKING:
    from .. import SmartRceConfigEntry
    from ..application.battery_day_service import BatteryDayService
    from ..application.pv_charge_service import PvChargeService

DATE_ENTITY = "input_datetime.energy_chart_date"


def build_battery_day_sensors(entry: SmartRceConfigEntry) -> list[SensorEntity]:
    return [BatteryDaySensor(entry)]


class BatteryDaySensor(SensorEntity):
    """The battery bands of the day picked on the dashboard."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:calendar-clock"
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._service: BatteryDayService = entry.runtime_data.battery_day
        self._pv_charge: PvChargeService = entry.runtime_data.pv_charge
        self._attr_unique_id = f"{DOMAIN}_ems_battery_day"
        self.entity_id = "sensor.ems_battery_day"
        self._attr_name = "EMS Battery Day"
        self._attr_device_info = ems_device_info(entry)
        self._attr_native_value: str | None = None
        self._attrs: dict[str, Any] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._pv_charge.async_add_listener(self._on_minute))
        self.async_on_remove(
            async_track_state_change_event(self.hass, [DATE_ENTITY], self._on_date)
        )
        await self._async_reload()

    @callback
    def _on_minute(self) -> None:
        # Only a live day moves with the clock; a past one stays as loaded.
        if self._service.is_live(self._selected_day()):
            self.hass.async_create_task(self._async_reload())

    @callback
    def _on_date(self, _event: Event[EventStateChangedData]) -> None:
        self.hass.async_create_task(self._async_reload())

    async def _async_reload(self) -> None:
        day = await self._service.async_day(self._selected_day())
        self._attr_native_value = day.source
        self._attrs = day.to_dict(dt_util.get_default_time_zone())
        self.async_write_ha_state()

    def _selected_day(self) -> date:
        state = self.hass.states.get(DATE_ENTITY)
        if state is not None:
            try:
                return date.fromisoformat(state.state)
            except ValueError:
                pass
        return dt_util.now().date()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self._attrs
