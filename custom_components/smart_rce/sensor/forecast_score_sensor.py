"""Forecast scoreboard sensors — headline figures with history, the day card, the ranking.

- Headline sensors: one number each of the last FINAL day — production's T1
  reserve error at 06:11 and its PV / consumption parts, the reserve line at
  08:41, the start error, and the facts (T1 import, min SoC, grid top-up,
  morning export). The clean daily trend is the external statistics
  `smart_rce:forecast_score_<key>` (`ForecastScoreStatistics`).
- `sensor.ems_forecast_score_day`: the card of the day picked on the
  dashboard (`input_datetime.energy_chart_date`, shared with the other Energy
  Hub views); today shows the preliminary or final score.
- `sensor.ems_forecast_score_ranking`: the 30-day ranking ending at that day.

The card and the ranking carry their tables as attributes, which the recorder
does not need (and which are too large for it): unrecorded.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import MATCH_ALL
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..domain.charge_start_shift import to_hhmm
from ..domain.forecast_score.headlines import HEADLINES, Headline
from ..domain.forecast_score.production_setup import setup_on
from ..domain.forecast_score.ranking import SHORT_WEIGHT
from ..ems_device import ems_device_info

if TYPE_CHECKING:
    from .. import SmartRceConfigEntry
    from ..application.forecast_score_service import ForecastScoreService
    from ..domain.forecast_score.day_score import DayScore

DATE_ENTITY: Final = "input_datetime.energy_chart_date"


_ICONS: Final[dict[str, str]] = {
    "reserve_error": "mdi:battery-alert-variant-outline",
    "reserve_error_pv": "mdi:solar-power-variant-outline",
    "reserve_error_consumption": "mdi:home-lightning-bolt-outline",
    "reserve_error_margin": "mdi:shield-plus-outline",
    "reserve_line_error": "mdi:battery-arrow-down-outline",
    "start_delta": "mdi:battery-clock-outline",
    "t1_import": "mdi:transmission-tower-import",
    "min_soc_t1": "mdi:battery-low",
    "grid_topup": "mdi:battery-charging-wireless-outline",
    "morning_export": "mdi:transmission-tower-export",
}


def build_forecast_score_sensors(entry: SmartRceConfigEntry) -> list[SensorEntity]:
    sensors: list[SensorEntity] = [
        ForecastScoreHeadlineSensor(entry, h) for h in HEADLINES
    ]
    sensors.append(ForecastScoreDaySensor(entry))
    sensors.append(ForecastScoreRankingSensor(entry))
    return sensors


class ForecastScoreHeadlineSensor(SensorEntity):
    """One headline figure of the last FINAL day (the daily series: external statistics)."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: SmartRceConfigEntry, headline: Headline) -> None:
        self._service: ForecastScoreService = entry.runtime_data.forecast_score
        self._headline = headline
        self._attr_unique_id = f"{DOMAIN}_ems_forecast_score_{headline.key}"
        self.entity_id = f"sensor.ems_forecast_score_{headline.key}"
        self._attr_name = f"EMS Forecast Score {headline.name}"
        self._attr_native_unit_of_measurement = headline.unit
        self._attr_icon = _ICONS.get(headline.key)
        self._attr_device_info = ems_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._service.async_add_listener(self.async_write_ha_state)
        )

    @property
    def native_value(self) -> float | int | None:
        latest = self._service.latest
        return self._headline.value(latest) if latest is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        latest = self._service.latest
        if latest is None:
            return None
        return {"day": latest.day.isoformat(), "status": str(latest.status)}


class _SelectedDaySensor(SensorEntity):
    """Follows the dashboard's date picker; reloads on a new date or a new score."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._service: ForecastScoreService = entry.runtime_data.forecast_score
        self._attr_device_info = ems_device_info(entry)
        self._attrs: dict[str, Any] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._service.async_add_listener(self._schedule_reload))
        self.async_on_remove(
            async_track_state_change_event(self.hass, [DATE_ENTITY], self._on_date)
        )
        await self._async_reload()

    @callback
    def _on_date(self, _event: Event[EventStateChangedData]) -> None:
        self._schedule_reload()

    @callback
    def _schedule_reload(self) -> None:
        self.hass.async_create_task(self._async_reload())

    async def _async_reload(self) -> None:
        await self._async_load(self._selected_day())
        self.async_write_ha_state()

    def _selected_day(self) -> date:
        state = self.hass.states.get(DATE_ENTITY)
        if state is not None:
            try:
                return date.fromisoformat(state.state)
            except ValueError:
                pass
        return dt_util.now().date()

    async def _async_load(self, day: date) -> None:
        raise NotImplementedError

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self._attrs


class ForecastScoreDaySensor(_SelectedDaySensor):
    """The scored day picked on the dashboard: state = its status."""

    _attr_icon = "mdi:scoreboard-outline"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{DOMAIN}_ems_forecast_score_day"
        self.entity_id = "sensor.ems_forecast_score_day"
        self._attr_name = "EMS Forecast Score Day"
        self._attr_native_value: str | None = None

    async def _async_load(self, day: date) -> None:
        score = await self._service.async_day(day)
        if score is None:
            self._attr_native_value = "missing"
            self._attrs = {
                "day": day.isoformat(),
                "message": "Brak wyniku dla tego dnia (ocena od 04.10.2026, "
                "wstępna o 16:06, ostateczna o 21:06).",
            }
            return
        self._attr_native_value = str(score.status)
        self._attrs = day_card(score)


class ForecastScoreRankingSensor(_SelectedDaySensor):
    """30-day ranking ending at the picked day: state = final days in it."""

    _attr_icon = "mdi:podium"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{DOMAIN}_ems_forecast_score_ranking"
        self.entity_id = "sensor.ems_forecast_score_ranking"
        self._attr_name = "EMS Forecast Score Ranking"
        self._attr_native_value: int | None = None

    async def _async_load(self, day: date) -> None:
        ranking = await self._service.async_ranking(day)
        self._attr_native_value = len(ranking["days"])
        self._attrs = {"end": day.isoformat(), **ranking}


def day_card(score: DayScore) -> dict[str, Any]:
    """Return the day's record plus the figures the card reads directly.

    Reserve errors are system − actual (− = the system asked for too little);
    start deltas system − benchmark (+ = too late).
    """
    setup = setup_on(score.day, dt_util.get_default_time_zone())
    return {
        **score.to_dict(),
        "day_kind": score.day_type.kind.value if score.day_type.kind else None,
        "headline": {h.key: h.value(score) for h in HEADLINES},
        "short_weight": SHORT_WEIGHT,
        "system": {
            "pv": setup.pv_label,
            "consumption": setup.model_label,
            "earlier_days": len(score.earlier_workdays),
            "buffer": setup.buffer_percent,
            "margin": setup.has_margin,
            "margin_rule": setup.margin.value,
            "hour_credit": setup.hour_credit,
        },
        "reserve_summary": [
            {
                "label": m.label,
                "actual_strict": m.needed_strict,
                "actual": m.production.needed if m.production else m.needed,
                "system": m.production.asked if m.production else None,
                "error": m.production_error_pp,
                "pv_part": m.pv_part_pp,
                "consumption_part": m.consumption_part_pp,
                "margin_part": m.margin_part_pp,
                "margin_pp": m.production.margin_pp if m.production else 0,
            }
            for m in score.reserve
        ],
        "start_summary": [
            {
                "label": m.label,
                "at": to_hhmm(m.minute),
                "soc": round(m.soc),
                "benchmark": to_hhmm(m.benchmark),
                "benchmark_enough": m.benchmark_enough,
                "system": (to_hhmm(m.production.call.start) if m.production else None),
                "verdict": str(m.production.call.verdict) if m.production else None,
                "delta_h": m.production.call.delta_h if m.production else None,
                "pv_part_h": m.pv_part_h,
                "consumption_part_h": m.consumption_part_h,
            }
            for m in score.start
        ],
        "outcome_summary": {
            "start": (
                to_hhmm(score.outcome.start.start)
                if score.outcome.start.start is not None
                else None
            ),
            "start_source": score.outcome.start.source,
            "s0": to_hhmm(score.s0) if score.s0 is not None else None,
            "full_at": (
                to_hhmm(score.outcome.full_at)
                if score.outcome.full_at is not None
                else None
            ),
            "grid_topup_kwh": score.outcome.grid_topup_kwh,
        },
    }
