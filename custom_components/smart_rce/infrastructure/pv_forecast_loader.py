"""PV Forecast loader — weather conditions builder (driving adapter).

Po Chunku 2 zostaje tylko `build_weather_conditions` — Solcast reading
przeniesione do `infrastructure/pv_forecast/solcast_reader.py`. W Chunku 3
ta funkcja też migruje do `infrastructure/pv_forecast/weather_conditions_builder.py`
i ten plik znika.
"""

from __future__ import annotations

from datetime import date, datetime
import logging
from typing import Any

from ..domain.pv_forecast import WeatherConditionAtHour
from ..weather_forecast_history import WeatherForecastHistory
from ..weather_listener import WeatherListenerCoordinator

_LOGGER = logging.getLogger(__name__)


def build_weather_conditions(
    weather_listener: WeatherListenerCoordinator,
    weather_forecast_history: WeatherForecastHistory,
    day: date | None,
) -> list[WeatherConditionAtHour]:
    """Build weather conditions from history (past hours) + forecast (future hours).

    History has conditions for hours that already passed today.
    Forecast has conditions for upcoming hours (possibly multiple days).
    Both have forecast_date for correct matching.
    """
    history_conditions: list[WeatherConditionAtHour] = []
    if day:
        history_conditions = weather_forecast_history.get_conditions_for_date(day)

    forecast_conditions = _parse_weather_conditions(weather_listener.forecast_hourly)

    # Combine: history first, forecast overwrites (forecast is more recent for future hours)
    combined: dict[tuple[date, int], WeatherConditionAtHour] = {}
    for c in history_conditions:
        if c.forecast_date:
            combined[(c.forecast_date, c.hour)] = c
    for c in forecast_conditions:
        if c.forecast_date:
            combined[(c.forecast_date, c.hour)] = c

    return list(combined.values())


def _parse_weather_conditions(
    forecast_hourly: list[dict[str, Any]] | None,
) -> list[WeatherConditionAtHour]:
    """Parse WeatherListenerCoordinator.forecast_hourly into domain objects."""
    if not forecast_hourly:
        return []
    return [
        WeatherConditionAtHour(
            hour=datetime.fromisoformat(item["datetime"]).hour,
            condition_custom=item.get("condition_custom", "cloudy"),
            forecast_date=datetime.fromisoformat(item["datetime"]).date(),
        )
        for item in forecast_hourly
        if "datetime" in item
    ]
