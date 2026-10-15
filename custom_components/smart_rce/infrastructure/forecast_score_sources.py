"""ForecastScoreSources — driven adapter gathering what a scored day is made of.

Everything the scoreboard reads lives somewhere else already:
- snapshots: the forecast archive;
- what came: the energy archive (00:15 for yesterday) or, for today, the
  recorder's 5-minute statistics (`fetch_energy_day`, the archive's own source);
- earlier days of consumption: production's own loaders, so the model scored
  is the model production used (workday calendar for the T1 reserve, the same
  kind of day for the charge start);
- prices: today's from the coordinator, earlier days' from the PSE API;
- S0: today's shift when it ran, else the charge-window rule on the prices;
- the start that struck and the afternoon charge spans: recorder history of
  `time.ems_battery_charge_start_hour_override` and
  `sensor.ems_battery_schedule_currently_engaging` (10 days of it).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.util import dt as dt_util

from ..domain.charge_slots import ChargeSlots
from ..domain.consumption_profiles import PREV_DAYS_COUNT
from ..domain.energy_day import EnergyDay
from ..domain.pv_forecast.forecast_snapshot import ForecastSnapshot
from ..domain.pv_surplus import DayType
from ..domain.rce import RceDayPrices
from .energy_archive_writer import fetch_energy_day

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, State

    from ..application.ems import Ems
    from ..domain.consumption_profiles import ConsumptionProfile
    from .daily_jsonl_archive import DailyJsonlArchive
    from .pv_forecast.consumption_profile_loader import ConsumptionProfileLoader
    from .pv_forecast.hourly_energy_loader import HourlyEnergyLoader
    from .rce_api import RceApi

START_ENTITY: Final = "time.ems_battery_charge_start_hour_override"
ENGAGING_ENTITY: Final = "sensor.ems_battery_schedule_currently_engaging"
AFTERNOON_SLOT: Final = "CHARGE_AFTERNOON"
_SNAPSHOT_KIND: Final = "forecast_snapshot"

_LOGGER = logging.getLogger(__name__)


class ForecastScoreSources:
    """Reads one day's inputs; every method is safe to call for any past day."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        forecast_archive: DailyJsonlArchive,
        energy_archive: DailyJsonlArchive,
        workday_loader: ConsumptionProfileLoader,
        same_type_loader: HourlyEnergyLoader,
        rce_api: RceApi,
        ems: Ems,
    ) -> None:
        self._hass = hass
        self._forecast = forecast_archive
        self._energy = energy_archive
        self._workday_loader = workday_loader
        self._same_type_loader = same_type_loader
        self._rce_api = rce_api
        self._ems = ems

    async def snapshots(self, day: date) -> list[ForecastSnapshot]:
        records = await self._hass.async_add_executor_job(self._forecast.read_day, day)
        return [
            ForecastSnapshot.from_dict(r)
            for r in records
            if r.get("kind") == _SNAPSHOT_KIND
        ]

    async def archived_energy(self, day: date) -> EnergyDay | None:
        records = await self._hass.async_add_executor_job(self._energy.read_day, day)
        return EnergyDay.from_records(records)

    async def recorder_energy(self, day: date) -> EnergyDay:
        return await fetch_energy_day(self._hass, day)

    async def earlier_workdays(self, day: date) -> dict[date, dict[int, float]]:
        """Read the T1 reserve's earlier workdays as production's loader does."""
        profiles = await self._workday_loader.fetch_for_anchor(day, PREV_DAYS_COUNT)
        return {
            p.source_date: _half_hours(p)
            for p in profiles
            if p is not None and p.source_date is not None
        }

    async def earlier_same_type(self, day: date) -> dict[date, dict[int, float]]:
        """Read the charge start's earlier days of the same type (production)."""
        days = DayType.of(day).previous_days(day)
        return await self._same_type_loader.consumption_by_day(days)

    async def prices(self, day: date) -> tuple[float, ...] | None:
        """RCE per hour; None when unknown or not a 24-hour day."""
        rce = self._ems.rce_prices.rce_prices
        found: RceDayPrices | None = None
        if rce is not None and rce.today is not None and rce.today.day == day:
            found = rce.today
        else:
            try:
                found = await self._rce_api.async_get_prices(
                    datetime.combine(day, time(0), tzinfo=dt_util.DEFAULT_TIME_ZONE)
                )
            except Exception:  # noqa: BLE001 — a missing price day scores without D2
                _LOGGER.exception("Forecast score: RCE prices of %s unavailable", day)
        if found is None or len(found.hour_price) != 24:
            return None
        return tuple(found.hour_price)

    def s0(self, day: date, prices: tuple[float, ...] | None) -> int | None:
        """Today's S0 from the shift when it ran; else the window rule on the prices."""
        shift = self._ems.battery_charge_service.forecast_shift
        if shift is not None and shift.day == day:
            return shift.s0
        if prices is None:
            return None
        window = ChargeSlots.compute(
            RceDayPrices(None, day, prices),
            params=self._ems.battery_charge_service.charge_window_params,
        )
        return round(window.start_hour * 60) if window is not None else None

    async def start_changes(self, day: date) -> list[tuple[int, int]] | None:
        """(minute set, start minute) of the start entity over `day`; None = no history."""
        states = await self._history(day, START_ENTITY)
        out: list[tuple[int, int]] = []
        for state in states:
            start = _parse_hhmm(state.state)
            if start is None:
                continue
            out.append((_minute_on(day, state.last_changed), start))
        return out or None

    async def afternoon_spans(self, day: date) -> list[tuple[int, int]] | None:
        """CHARGE_AFTERNOON engagements over `day` (minutes); None = no history."""
        states = await self._history(day, ENGAGING_ENTITY)
        if not states:
            return None
        spans: list[tuple[int, int]] = []
        begin: int | None = None
        for state in states:
            minute = _minute_on(day, state.last_changed)
            if state.state == AFTERNOON_SLOT and begin is None:
                begin = minute
            elif state.state != AFTERNOON_SLOT and begin is not None:
                spans.append((begin, minute))
                begin = None
        if begin is not None:
            end = min(_minute_on(day, dt_util.now()), 24 * 60)
            spans.append((begin, end))
        return spans

    async def _history(self, day: date, entity_id: str) -> list[State]:
        tz = dt_util.DEFAULT_TIME_ZONE
        start = datetime.combine(day, time(0), tzinfo=tz)
        end = min(start + timedelta(days=1), dt_util.now())
        result: dict[str, list[Any]] = await get_instance(
            self._hass
        ).async_add_executor_job(
            state_changes_during_period, self._hass, start, end, entity_id, True
        )
        return [
            s
            for s in result.get(entity_id, [])
            if s.state not in {"unknown", "unavailable"}
        ]


def _half_hours(profile: ConsumptionProfile) -> dict[int, float]:
    return {b.hour * 2 + b.minute // 30: kwh for b, kwh in profile.buckets.items()}


def _minute_on(day: date, moment: datetime) -> int:
    """Minutes since local midnight of `day`; the start state carried over is 0."""
    local = dt_util.as_local(moment)
    if local.date() < day:
        return 0
    return local.hour * 60 + local.minute


def _parse_hhmm(raw: str) -> int | None:
    try:
        value = time.fromisoformat(raw)
    except ValueError:
        return None
    return value.hour * 60 + value.minute
