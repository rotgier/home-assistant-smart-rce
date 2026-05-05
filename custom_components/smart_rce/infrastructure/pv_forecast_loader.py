"""PV Forecast loader — Solcast/weather sources (driving adapter).

Reads Solcast forecast attributes from HA state machine + builds combined
weather conditions (history + forecast) for matching against PV periods.

Hexagonal pattern: **driving adapter (inbound)** — infrastructure boundary
adapting HA `states` API + WeatherListenerCoordinator/WeatherForecastHistory
into pure domain types (`SolcastPeriod`, `WeatherConditionAtHour`) consumed
by `application.pv_forecast_service.PvForecastService`.
"""

from __future__ import annotations

from datetime import date, datetime
import logging
from typing import Any, Final

from homeassistant.core import HomeAssistant

from ..domain.pv_forecast import SolcastPeriod, WeatherConditionAtHour
from ..weather_forecast_history import WeatherForecastHistory
from ..weather_listener import WeatherListenerCoordinator

SOLCAST_AT_6_ENTITY: Final = "sensor.solcast_forecast_at_6"
SOLCAST_LIVE_ENTITY: Final = "sensor.solcast_pv_forecast_prognoza_na_dzisiaj"
SOLCAST_TOMORROW_ENTITY: Final = "sensor.solcast_pv_forecast_prognoza_na_jutro"

_LOGGER = logging.getLogger(__name__)


def read_solcast_periods(
    hass: HomeAssistant, entity_id: str, attr_name: str
) -> list[SolcastPeriod] | None:
    """Read Solcast forecast from HA state machine."""
    state = hass.states.get(entity_id)
    if not state:
        _LOGGER.debug("Entity %s not found", entity_id)
        return None

    forecast_attr = state.attributes.get(attr_name)
    if not forecast_attr:
        _LOGGER.debug("Entity %s has no attribute %s", entity_id, attr_name)
        return None

    return _parse_solcast_forecast(forecast_attr)


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


def _parse_solcast_forecast(
    forecast_attr: list[dict[str, Any]],
) -> list[SolcastPeriod]:
    """Parse Solcast forecast attribute into domain objects."""
    return [
        SolcastPeriod(
            period_start=str(item["period_start"]),
            pv_estimate=item["pv_estimate"],
            pv_estimate10=item["pv_estimate10"],
            pv_estimate90=item["pv_estimate90"],
        )
        for item in forecast_attr
    ]


def _parse_weather_conditions(
    forecast_hourly: list[dict[str, Any]] | None,
) -> list[WeatherConditionAtHour]:
    """Parse WeatherListenerCoordinator.forecast_hourly into domain objects.

    Returns conditions with both hour and date, to allow matching
    against the correct day in Solcast forecast.
    """
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
