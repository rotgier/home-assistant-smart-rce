"""Public API for the PV forecast domain.

Re-exports the framework + concrete strategy classes + orchestrator so
consumers can keep `from .domain.pv_forecast import X` style imports.
"""

from .forecast_enum import PvForecast
from .forecasts import PvForecasts
from .strategies_extrapolation import (
    ExtrapBandRecentStrategy,
    ExtrapBandStrategy,
    ExtrapBlendStrategy,
    ExtrapPatternStrategy,
    ExtrapProportionalCappedStrategy,
    ExtrapProportionalStrategy,
)
from .strategies_weather import At6Strategy, LiveSoftStrategy, LiveStrategy
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
    "ExtrapBlendStrategy",
    "ExtrapBandStrategy",
    "ExtrapPatternStrategy",
    "ExtrapProportionalCappedStrategy",
    "ExtrapProportionalStrategy",
    "ForecastContext",
    "ForecastStrategy",
    "LivePvSignals",
    "LiveSoftStrategy",
    "LiveStrategy",
    "PvForecast",
    "PvForecastResult",
    "PvForecasts",
    "SolcastPeriod",
    "WeatherConditionAtHour",
    "WeatherConditions",
]
