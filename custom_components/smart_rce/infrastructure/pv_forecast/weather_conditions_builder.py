"""WeatherConditionsBuilder — driving adapter łączący weather history + forecast.

Combines historical conditions (past hours dziś, "frozen" w `WeatherForecastHistory`)
z live forecast (future hours, z `WeatherListenerCoordinator`) — produces
domain `WeatherConditionAtHour` list dla matching against Solcast periods.

Hexagonal pattern: **driving adapter (inbound)** — wraps two HA-adjacent
components (history tracker + listener coordinator) into single domain-typed
output.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ...domain.pv_forecast import WeatherConditionAtHour
from ...weather_forecast_history import WeatherForecastHistory
from ...weather_listener import WeatherListenerCoordinator


class WeatherConditionsBuilder:
    """Builds combined weather conditions z history (past) + forecast (future)."""

    def __init__(
        self,
        weather_listener: WeatherListenerCoordinator,
        weather_forecast_history: WeatherForecastHistory,
    ) -> None:
        self._weather_listener = weather_listener
        self._weather_forecast_history = weather_forecast_history

    def build(self, day: date | None) -> list[WeatherConditionAtHour]:
        """Combine: history first, forecast overwrites (forecast = more recent for future)."""
        history_conditions: list[WeatherConditionAtHour] = []
        if day:
            history_conditions = self._weather_forecast_history.get_conditions_for_date(
                day
            )

        forecast_conditions = _parse_weather_conditions(
            self._weather_listener.forecast_hourly
        )

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
