"""WeatherHistoryLoader — driven adapter for recorder reads of wetteronline sensors.

Fetches state-change history for the 8 wetteronline sensors during a target
date through the recorder API. Used by `WeatherTableService` to assemble
the dashboard weather table for past dates (and the past portion of today).

Hexagonal pattern: **driven adapter (outbound)** — application service
dictates "give me state changes for these sensors during this day", the
concrete impl uses HA recorder.

Returns `dict[entity_id, list[StateSnapshot]]` keyed by the wetteronline
entity_ids defined in `domain/weather_table.py::WETTERONLINE_SENSORS`.
Domain `StateSnapshot` is a thin (timestamp, raw_state) named tuple — the
recorder's `State` object is translated at this seam so domain code stays
HA-free.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from ..domain.weather_table import WETTERONLINE_SENSORS, StateSnapshot

# How long to wait for the recorder to flush pending state_changed writes
# before querying. Recorder batches inserts, so the rows for state changes
# that just happened may still be in queue. Bounded so the sensor never
# hangs (vs. unconditional async_block_till_done which can block for
# minutes at startup while recorder restores state).
_RECORDER_FLUSH_TIMEOUT_S = 2.0

_LOGGER = logging.getLogger(__name__)


class WeatherHistoryLoader:
    """Loads state-change history for wetteronline sensors per target date."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def fetch(
        self,
        target_date: date,
        end_time: datetime | None = None,
    ) -> dict[str, list[StateSnapshot]]:
        """Fetch state changes for all 8 wetteronline sensors during target_date.

        Uses `state_changes_during_period` per entity (sequential inside a
        single executor job). Tried `get_significant_states` first but it
        truncated the result around HA Core restart boundaries — switching
        to `state_changes_during_period` returns every state_changed event
        as expected.

        `end_time` (optional) clips the window — used by snapshot mode to
        view history "as it was" at a specific moment, dropping any state
        changes recorded after that point.
        """
        tz = dt_util.DEFAULT_TIME_ZONE
        start = datetime.combine(target_date, time(0, 0), tzinfo=tz)
        eod = datetime.combine(target_date, time(23, 59, 59, 999_999), tzinfo=tz)
        end = min(end_time, eod) if end_time is not None else eod

        instance = get_instance(self._hass)
        # Flush pending recorder writes before reading. Without this the
        # query may miss state changes that just happened (e.g., the same
        # coordinator-update event that triggered THIS recompute) because
        # recorder batches inserts. Bounded by a short timeout — at HA
        # startup the recorder restores many entities and an unbounded
        # async_block_till_done would block the sensor recompute for
        # minutes (observed: ~5 min on cold boot before the first table
        # surfaced). At steady state the wait usually returns in <1s.
        try:
            await asyncio.wait_for(
                instance.async_block_till_done(),
                timeout=_RECORDER_FLUSH_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            _LOGGER.debug(
                "Recorder flush timed out after %ss; proceeding with possibly "
                "stale data (next refresh will catch up)",
                _RECORDER_FLUSH_TIMEOUT_S,
            )
        raw: dict[str, list[State]] = await instance.async_add_executor_job(
            self._fetch_sync, start, end
        )

        out: dict[str, list[StateSnapshot]] = {}
        for entity_id in WETTERONLINE_SENSORS:
            out[entity_id] = [
                _to_snapshot(state, tz)
                for state in raw.get(entity_id, [])
                if state.last_changed.astimezone(tz).date() == target_date
            ]
        total = sum(len(snaps) for snaps in out.values())
        _LOGGER.debug(
            "WeatherHistoryLoader: %d total state changes across %d sensors for %s",
            total,
            len(WETTERONLINE_SENSORS),
            target_date,
        )
        return out

    def _fetch_sync(self, start: datetime, end: datetime) -> dict[str, list[Any]]:
        out: dict[str, list[Any]] = {}
        for entity_id in WETTERONLINE_SENSORS:
            result = state_changes_during_period(
                self._hass,
                start,
                end,
                entity_id=entity_id,
                no_attributes=True,
                include_start_time_state=True,
            )
            out[entity_id] = result.get(entity_id, [])
        return out

    def _fetch_forecast_at_sync(
        self, start: datetime, end: datetime, entity_id: str
    ) -> dict[str, list[State]]:
        return state_changes_during_period(
            self._hass,
            start,
            end,
            entity_id=entity_id,
            no_attributes=False,
            include_start_time_state=True,
        )

    async def fetch_forecast_at(
        self,
        snapshot_time: datetime,
        sensor_entity_id: str = "sensor.wetteronline_forecast_for_today",
    ) -> list[dict[str, Any]]:
        """Fetch the `forecast` attribute of `sensor_entity_id` as it was at `snapshot_time`.

        Returns the slot list from the most recent state recorded at or
        before `snapshot_time`. Empty list when no history is available
        (snapshot older than recorder retention, or sensor wasn't deployed
        yet at that moment).

        Uses a tiny `[snapshot_time, snapshot_time + 1s]` window with
        `include_start_time_state=True` — HA recorder returns a boundary
        row projecting the state as it was at `snapshot_time` (last
        state_changed event with `last_updated_ts <= snapshot_time`).
        """
        instance = get_instance(self._hass)
        try:
            await asyncio.wait_for(
                instance.async_block_till_done(),
                timeout=_RECORDER_FLUSH_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            _LOGGER.debug("fetch_forecast_at: recorder flush timed out, proceeding")

        end = snapshot_time + timedelta(seconds=1)
        raw: dict[str, list[State]] = await instance.async_add_executor_job(
            self._fetch_forecast_at_sync, snapshot_time, end, sensor_entity_id
        )
        rows = raw.get(sensor_entity_id, [])
        if not rows:
            _LOGGER.debug(
                "fetch_forecast_at: no history for %s at %s",
                sensor_entity_id,
                snapshot_time,
            )
            return []
        boundary = rows[0]
        slots = boundary.attributes.get("forecast") or []
        if not isinstance(slots, list):
            return []
        return [s for s in slots if isinstance(s, dict)]


def _to_snapshot(state: State, tz: Any) -> StateSnapshot:
    """Translate HA `State` to domain `StateSnapshot`.

    Map HA's "unknown"/"unavailable" sentinels to None so domain dedupe
    logic treats them uniformly with truly-missing sensors.

    `state.last_changed` is UTC out of the recorder; convert to the
    configured timezone so domain code emits row datetimes/HH:MM labels
    in the user's wall-clock time.
    """
    value: str | None = state.state
    if value in ("unknown", "unavailable", ""):
        value = None
    return StateSnapshot(timestamp=state.last_changed.astimezone(tz), value=value)
