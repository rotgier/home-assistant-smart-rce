"""Forecast reader — weather.wetteronline `forecast_hourly` → garden slots.

`ForecastReader` is the driving adapter: it owns the ems-published
`HourlyForecastProvider` port (cross-context contract, see
`application/hourly_forecast.py`), so the application layer never sees the
provider — it calls `read_forecast_slots()` and subscribes via `subscribe()`.

Mapping lives in `parse_forecast_slots` (pure, unit-tested directly): each hour
expands to its 15-min `nowcast_15min` slots when present, otherwise becomes a
single 60-min slot — mirroring the legacy Jinja planner's slot build. Past
nowcast slots are already dropped at the wetteronline source.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from custom_components.smart_rce.garden.domain.forecast_window import ForecastSlot

if TYPE_CHECKING:
    from collections.abc import Callable

    from custom_components.smart_rce.application.hourly_forecast import (
        HourlyForecastProvider,
    )
    from homeassistant.core import CALLBACK_TYPE


class ForecastReader:
    """Reads + watches the hourly forecast (owns the ems-published port)."""

    def __init__(self, forecast: HourlyForecastProvider) -> None:
        self._forecast = forecast

    def read_forecast_slots(self) -> list[ForecastSlot]:
        """Return the current forecast as normalized domain slots."""
        return parse_forecast_slots(self._forecast.forecast_hourly)

    def subscribe(self, on_change: CALLBACK_TYPE) -> Callable[[], None]:
        """Invoke `on_change` on forecast updates; returns unsubscribe."""
        return self._forecast.async_add_listener(on_change)


_NOWCAST_SLOT = timedelta(minutes=15)
_HOURLY_SLOT = timedelta(minutes=60)


def parse_forecast_slots(forecast_hourly: list[Any] | None) -> list[ForecastSlot]:
    """Map `forecast_hourly` entries into a flat list of `ForecastSlot`."""
    slots: list[ForecastSlot] = []
    for hour in forecast_hourly or []:
        if not isinstance(hour, dict):
            continue
        nowcast = hour.get("nowcast_15min") or []
        if nowcast:
            slots.extend(_nowcast_slots(nowcast))
        else:
            slot = _slot(
                hour.get("datetime"),
                hour.get("precipitation_probability"),
                _HOURLY_SLOT,
            )
            if slot is not None:
                slots.append(slot)
    return slots


def _nowcast_slots(nowcast: list[Any]) -> list[ForecastSlot]:
    out: list[ForecastSlot] = []
    for item in nowcast:
        if not isinstance(item, dict):
            continue
        slot = _slot(
            item.get("date"),
            item.get("precipitation_probability"),
            _NOWCAST_SLOT,
        )
        if slot is not None:
            out.append(slot)
    return out


def _slot(iso: Any, prob: Any, duration: timedelta) -> ForecastSlot | None:
    if not isinstance(iso, str):
        return None
    try:
        start = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return ForecastSlot(start=start, rain_prob=int(prob or 0), duration=duration)
