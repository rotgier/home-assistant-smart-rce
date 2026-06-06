"""`PvForecast` enum — all 8 variants + axis classmethods.

Each enum member declares at the source: string key, bound `ForecastStrategy`
instance, `is_today` (date axis), `is_extrap` (source kind). Consumers
iterate partitions via `PvForecast.today()` / `.tomorrow()` / `.extrap()`.

This file imports the concrete strategy classes; sibling strategy files
must not import the enum at module top level (would create a cycle).
EXTRAP `_compute` does a lazy `from .forecast_enum import PvForecast`
to read `PvForecast.LIVE.result`.
"""

from __future__ import annotations

from enum import Enum

from .strategies_extrapolation import (
    ExtrapBandRecentStrategy,
    ExtrapBandStrategy,
    ExtrapPatternStrategy,
    ExtrapProportionalStrategy,
)
from .strategies_weather import At6Strategy, LiveStrategy
from .strategy_base import ForecastStrategy, PvForecastResult


class PvForecast(Enum):
    """All PV forecast variants — key + bound strategy + axis flags.

    Each member declares at the source: string key, ForecastStrategy
    instance, `is_today` (date axis), `is_extrap` (source/computation
    kind). Consumers iterate partitions via `PvForecast.today()` /
    `.tomorrow()` / `.extrap()` classmethods.

    Naming convention: `<date_axis>_<source>` where source ∈ {at_6, live,
    extrap_*}. Today's variants drop the date prefix (implicit).
    """

    AT_6 = ("at_6", At6Strategy(today=True), True, False)
    LIVE = ("live", LiveStrategy(today=True), True, False)
    TOMORROW_AT_6 = ("tomorrow_at_6", At6Strategy(today=False), False, False)
    TOMORROW_LIVE = ("tomorrow_live", LiveStrategy(today=False), False, False)
    EXTRAP_PATTERN = ("extrapolated_live_pattern", ExtrapPatternStrategy(), True, True)
    EXTRAP_PROPORTIONAL = (
        "extrapolated_live_proportional",
        ExtrapProportionalStrategy(),
        True,
        True,
    )
    EXTRAP_BAND = ("extrapolated_live_band", ExtrapBandStrategy(), True, True)
    EXTRAP_BAND_RECENT = (
        "extrapolated_live_band_recent",
        ExtrapBandRecentStrategy(),
        True,
        True,
    )

    def __init__(
        self,
        key: str,
        strategy: ForecastStrategy,
        is_today: bool,
        is_extrap: bool,
    ) -> None:
        self.key = key
        self.strategy = strategy
        self.is_today = is_today
        self.is_extrap = is_extrap

    @property
    def is_tomorrow(self) -> bool:
        return not self.is_today

    @property
    def result(self) -> PvForecastResult | None:
        """Current forecast result — from bound strategy."""
        return self.strategy.result

    @classmethod
    def today(cls) -> tuple[PvForecast, ...]:
        """Today-axis variants (AT_6 + LIVE + 4× EXTRAP)."""
        return tuple(v for v in cls if v.is_today)

    @classmethod
    def tomorrow(cls) -> tuple[PvForecast, ...]:
        """Tomorrow-axis variants (TOMORROW_AT_6 + TOMORROW_LIVE)."""
        return tuple(v for v in cls if v.is_tomorrow)

    @classmethod
    def extrap(cls) -> tuple[PvForecast, ...]:
        """EXTRAP variants (4× extrapolated-from-LIVE)."""
        return tuple(v for v in cls if v.is_extrap)
