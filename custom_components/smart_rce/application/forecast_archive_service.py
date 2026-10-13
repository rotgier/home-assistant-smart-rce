"""ForecastArchiveService — archives PV forecast snapshots at the moments that matter.

Moments (decided by `SnapshotSchedule`):
- 20 s after a successful Solcast API fetch — both forecast sources are fresh
  then, and the delay lets `EnergyBalanceService` re-dispatch every variant;
- the half-hourly grid (:11:30 / :41:30), only when no fetch-driven snapshot
  is pending or recent;
- the opening of the morning charge window, a decision taken on the forecast.

Writing is fire-and-forget on the executor: a failed write is logged and must
never disturb the forecast pipeline it observes.
"""

from __future__ import annotations

from datetime import date, datetime
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from ..domain.pv_forecast.forecast_snapshot import ForecastSnapshot, SnapshotTrigger
from ..domain.pv_forecast.snapshot_schedule import SnapshotSchedule

if TYPE_CHECKING:
    from homeassistant.core import Event, EventStateChangedData, HomeAssistant

    from ..domain.pv_forecast import PvForecasts
    from ..infrastructure.daily_jsonl_archive import DailyJsonlArchive
    from ..infrastructure.pv_forecast.solcast_reader import SolcastReader

_LOGGER = logging.getLogger(__name__)


class ForecastArchiveService:
    """Decides when to snapshot the forecasts and appends snapshots to the archive."""

    # Time for the Solcast-driven re-dispatch to finish before capturing.
    SOLCAST_SETTLE_SEC: Final = 20.0

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        forecasts: PvForecasts,
        solcast: SolcastReader,
        archive: DailyJsonlArchive,
        code_version: str,
    ) -> None:
        self._hass = hass
        self._forecasts = forecasts
        self._solcast = solcast
        self._archive = archive
        self._code_version = code_version
        self._schedule = SnapshotSchedule()
        self._cancel_settle: CALLBACK_TYPE | None = None

    async def async_start(self) -> None:
        """Close days a restart left open; resume the schedule from today's records."""
        today = dt_util.now().date()
        await self.async_close_finished_days()
        records = await self._hass.async_add_executor_job(self._archive.read_day, today)
        last = records[-1] if records else None
        self._schedule.resume(
            last_taken_at=datetime.fromisoformat(last["taken_at"]) if last else None,
            last_fetched_at=(
                last.get("solcast_fetched_at")
                if last
                else self._solcast.read_api_update()
            ),
        )

    async def async_close_finished_days(self) -> None:
        today = dt_util.now().date()
        try:
            closed = await self._hass.async_add_executor_job(
                self._archive.close_days_before, today
            )
        except OSError:
            _LOGGER.exception("Forecast archive: closing finished days failed")
            return
        if closed:
            _LOGGER.debug("Forecast archive: closed %s", closed)

    @callback
    def on_solcast_api_update(self, event: Event[EventStateChangedData]) -> None:
        """Solcast "last API update" changed — snapshot shortly, if it is a new fetch."""
        new_state = event.data["new_state"]
        value = new_state.state if new_state is not None else None
        if event.data["old_state"] is None:
            # Sensor (re)created on startup: the value is old, not a fetch.
            self._schedule.remember_fetch(value)
            return
        if not self._schedule.accept_fetch(value):
            return
        if self._cancel_settle is not None:
            self._cancel_settle()
        self._cancel_settle = async_call_later(
            self._hass, self.SOLCAST_SETTLE_SEC, self._on_solcast_settled
        )

    @callback
    def _on_solcast_settled(self, _now: datetime) -> None:
        self._cancel_settle = None
        self._take(SnapshotTrigger.SOLCAST)
        self._schedule.fetch_handled()

    @callback
    def on_grid_tick(self, now: datetime) -> None:
        if self._schedule.grid_due(now):
            self._take(SnapshotTrigger.GRID)

    @callback
    def on_morning_charge(self, _now: datetime) -> None:
        self._take(SnapshotTrigger.MORNING_CHARGE)

    def _take(self, trigger: SnapshotTrigger) -> None:
        now = dt_util.now()
        snapshot = ForecastSnapshot.capture(
            self._forecasts,
            taken_at=now,
            trigger=trigger,
            solcast_fetched_at=self._solcast.read_api_update(),
            code_version=self._code_version,
        )
        if snapshot is None:
            _LOGGER.debug("Forecast archive: no dispatch yet, %s skipped", trigger)
            return
        self._schedule.taken(now)
        self._hass.async_create_task(self._async_append(now.date(), snapshot.to_dict()))

    async def _async_append(self, day: date, record: dict[str, Any]) -> None:
        try:
            await self._hass.async_add_executor_job(self._archive.append, day, record)
        except OSError:
            _LOGGER.exception("Forecast archive: writing a snapshot failed")
