"""WeatherTableService — application service orchestrating dashboard weather table.

DDD application layer (analog of `energy_balance_service.py`): pulls history
from the recorder (driving adapter `WeatherHistoryLoader`), reads the live
forecast attribute via `WeatherForecastListener.forecast_hourly` (already
subscribed in smart_rce), and delegates row assembly + dedupe to the pure
domain function `weather_table.assemble_rows`.

Returns a dict shaped for the smart_rce service response and for the
table-bridging sensor attribute:

```python
{
    "date": "2026-05-12",
    "rows": [
        {datetime, time, source, condition_custom, ..., multiplier, ...},
        ...
    ],
}
```
"""

from __future__ import annotations

from datetime import date, datetime
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..domain.weather_table import assemble_rows
from ..infrastructure.weather_history_loader import WeatherHistoryLoader
from ..infrastructure.weather_listener import WeatherForecastListener

_LOGGER = logging.getLogger(__name__)


class WeatherTableService:
    """Assemble dashboard weather table rows for a target date."""

    def __init__(
        self,
        hass: HomeAssistant,
        history_loader: WeatherHistoryLoader,
        weather_listener: WeatherForecastListener,
    ) -> None:
        self._hass = hass
        self._history_loader = history_loader
        self._weather_listener = weather_listener

    async def async_get_table(
        self,
        target_date: date,
        snapshot_time: datetime | None = None,
    ) -> dict[str, Any]:
        """Build and return the assembled table for `target_date`.

        Live mode (`snapshot_time=None`): uses live `weather_listener.forecast_hourly`
        plus full-day history from recorder. Reflects current state.

        Snapshot mode (`snapshot_time` set): reconstructs the table as it
        would have looked at `snapshot_time` — history clipped to that
        moment, forecast attribute fetched from the historic state of
        `sensor.wetteronline_forecast_for_today`. Used by the
        Target SOC Historical dashboard tab.
        """
        history = await self._history_loader.fetch(target_date, end_time=snapshot_time)
        now = snapshot_time if snapshot_time is not None else dt_util.now()
        if snapshot_time is not None:
            forecast_raw = await self._history_loader.fetch_forecast_at(snapshot_time)
        else:
            forecast_raw = list(self._weather_listener.forecast_hourly or [])

        current_obs: dict[str, Any] | None = None
        future_hours: list[dict[str, Any]] = []
        nowcast_items: list[dict[str, Any]] = []

        if forecast_raw:
            if target_date == now.date():
                # forecast[0] is the synthesized current hour (wetteronline's
                # weather entity prepends it). Treat it as the `current` row
                # source; the rest are future hours.
                current_obs = _ensure_dict(forecast_raw[0])
                future_hours = [_ensure_dict(h) for h in forecast_raw[1:]]
                # Flatten nowcast_15min across all forecast items — they may
                # appear on the synthesized hour and the next 1-2 hours.
                for h in forecast_raw:
                    items = _ensure_dict(h).get("nowcast_15min", [])
                    if isinstance(items, list):
                        nowcast_items.extend(items)
            else:
                # Future date (within forecast horizon, ~2 days ahead): pass
                # every forecast hour — domain's `_forecast_rows` filters by
                # target_date internally. No `current` row (live snapshot
                # only makes sense for today) and no nowcast (covers only
                # the next ~105 min, never a different day).
                future_hours = [_ensure_dict(h) for h in forecast_raw]

        rows = assemble_rows(
            history_per_sensor=history,
            target_date=target_date,
            now=now,
            current_obs=current_obs,
            forecast_hours=future_hours,
            nowcast_items=nowcast_items,
            tz=dt_util.DEFAULT_TIME_ZONE,
        )
        _LOGGER.debug(
            "WeatherTableService: %d rows assembled for %s (snapshot=%s)",
            len(rows),
            target_date,
            snapshot_time.isoformat() if snapshot_time else "live",
        )
        return {
            "date": target_date.isoformat(),
            "rows": rows,
            "snapshot_time": snapshot_time.isoformat() if snapshot_time else None,
        }


def _ensure_dict(item: Any) -> dict[str, Any]:
    """Defensive: forecast list items are dicts but JsonValueType is broader."""
    return item if isinstance(item, dict) else {}
