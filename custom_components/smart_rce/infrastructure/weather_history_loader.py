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

from datetime import date, datetime, time
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from ..domain.weather_table import WETTERONLINE_SENSORS, StateSnapshot

_LOGGER = logging.getLogger(__name__)


class WeatherHistoryLoader:
    """Loads state-change history for wetteronline sensors per target date."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def fetch(self, target_date: date) -> dict[str, list[StateSnapshot]]:
        """Fetch state changes for all 8 wetteronline sensors during target_date.

        `get_significant_states(significant_changes_only=False)` returns
        every recorded state change (not just "significant" thresholded
        ones), which is what we need for the row-per-change table view.
        """
        tz = dt_util.DEFAULT_TIME_ZONE
        start = datetime.combine(target_date, time(0, 0), tzinfo=tz)
        end = datetime.combine(target_date, time(23, 59, 59, 999_999), tzinfo=tz)

        instance = get_instance(self._hass)
        raw: dict[str, list[State]] = await instance.async_add_executor_job(
            self._fetch_sync, start, end
        )

        out: dict[str, list[StateSnapshot]] = {}
        for entity_id in WETTERONLINE_SENSORS:
            out[entity_id] = [
                _to_snapshot(state)
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
        return get_significant_states(
            self._hass,
            start,
            end,
            entity_ids=list(WETTERONLINE_SENSORS),
            significant_changes_only=False,
            include_start_time_state=True,
            minimal_response=False,
            no_attributes=True,
        )


def _to_snapshot(state: State) -> StateSnapshot:
    """Translate HA `State` to domain `StateSnapshot`.

    Map HA's "unknown"/"unavailable" sentinels to None so domain dedupe
    logic treats them uniformly with truly-missing sensors.
    """
    value: str | None = state.state
    if value in ("unknown", "unavailable", ""):
        value = None
    return StateSnapshot(timestamp=state.last_changed, value=value)
