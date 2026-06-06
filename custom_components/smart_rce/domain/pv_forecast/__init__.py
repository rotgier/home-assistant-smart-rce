"""Public API for the PV forecast domain.

Re-exports the framework + concrete strategy classes + orchestrator so
consumers can keep `from .domain.pv_forecast import X` style imports.
"""

from .forecast_enum import PvForecast
from .forecasts import PvForecasts
from .strategies_extrapolation import (
    ExtrapBandRecentStrategy,
    ExtrapBandStrategy,
    ExtrapPatternStrategy,
    ExtrapProportionalStrategy,
)
from .strategies_weather import At6Strategy, LiveStrategy
from .strategy_base import (
    AdjustedPeriod,
    ForecastContext,
    ForecastStrategy,
    LivePvSignals,
    PvForecastResult,
    SolcastPeriod,
    WeatherConditionAtHour,
    WeatherConditions,
)

__all__ = [
    "AdjustedPeriod",
    "At6Strategy",
    "ExtrapBandRecentStrategy",
    "ExtrapBandStrategy",
    "ExtrapPatternStrategy",
    "ExtrapProportionalStrategy",
    "ForecastContext",
    "ForecastStrategy",
    "LivePvSignals",
    "LiveStrategy",
    "PvForecast",
    "PvForecastResult",
    "PvForecasts",
    "SolcastPeriod",
    "WeatherConditionAtHour",
    "WeatherConditions",
]
