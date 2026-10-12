"""Forecast-driven charge start — today's start and what each variant asked for.

State: today's start of taking PV into the battery as the forecast set it
(S0 from prices until the first move). Attributes carry the last evaluation —
candidates of the four variants and where each would have put the start — so
the variants can be compared over 2–3 weeks before one is chosen for good.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback

from ..const import DOMAIN
from ..domain.charge_start_shift import ShiftVariant, to_hhmm
from ..ems_device import ems_device_info

if TYPE_CHECKING:
    from .. import SmartRceConfigEntry
    from ..application.charge_start_shift_service import ChargeStartShiftService


def build_charge_start_shift_sensors(entry: SmartRceConfigEntry) -> list[SensorEntity]:
    return [ChargeStartShiftSensor(entry)]


class ChargeStartShiftSensor(SensorEntity):
    """Today's forecast-driven charge start (HH:MM)."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:battery-clock"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._service: ChargeStartShiftService = entry.runtime_data.charge_start_shift
        self._attr_unique_id = f"{DOMAIN}_ems_charge_start_forecast"
        self.entity_id = "sensor.ems_charge_start_forecast"
        self._attr_name = "EMS Charge Start Forecast"
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.async_add_listener(self._on_update))

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        shift = self._service.shift
        return to_hhmm(shift.start) if shift is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        shift = self._service.shift
        if shift is None:
            return None
        attrs: dict[str, Any] = {
            "day": shift.day.isoformat(),
            "s0": to_hhmm(shift.s0),
            "shifted": shift.is_shifted,
            "locked": shift.locked,
            "soc_at_start": shift.soc_at_start,
            "recent": [to_hhmm(c) for c in shift.recent],
            "manual_start": self._service.manual_start_today,
        }
        last = self._service.last
        if last is not None and last.at.date() == shift.day:
            attrs["evaluated_at"] = last.at.isoformat()
            attrs["soc"] = last.soc
            attrs["need_kwh"] = round(last.need_kwh, 2)
            attrs["variants"] = {
                variant.key: {
                    "candidate": _hm_or_none(last.candidates.get(variant)),
                    "start": _hm_or_none(self._service.observed_start(variant)),
                }
                for variant in ShiftVariant
            }
        return attrs


def _hm_or_none(minutes: int | None) -> str | None:
    return None if minutes is None else to_hhmm(minutes)
