"""`PvForecast` enum — all 12 variants + axis classmethods.

Each enum member declares at the source: string key + bound `ForecastStrategy`
instance. Axis flags (`is_today`, `is_extrap`) live on the strategy itself —
enum's properties delegate, no duplication in tuple values. Consumers iterate
partitions via `PvForecast.today()` / `.tomorrow()` / `.extrap()`.

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
    ExtrapBlendStrategy,
    ExtrapPatternStrategy,
    ExtrapProportionalCappedStrategy,
    ExtrapProportionalStrategy,
)
from .strategies_weather import At6Strategy, LiveSoftStrategy, LiveStrategy
from .strategy_base import ForecastStrategy, PvForecastResult


class PvForecast(Enum):
    """All PV forecast variants — string key + bound `ForecastStrategy`.

    Axis flags are read from the bound strategy (`is_today` set per ctor arg
    on At6/LiveStrategy; `is_extrap=True` class attr on _ExtrapStrategyBase).
    Consumers iterate partitions via `PvForecast.today()` / `.tomorrow()` /
    `.extrap()` classmethods.

    Naming convention: `<date_axis>_<source>` where source ∈ {at_6, live,
    live_soft, extrap_*}. Today's variants drop the date prefix (implicit).
    """

    AT_6 = ("at_6", At6Strategy(today=True))
    LIVE = ("live", LiveStrategy(today=True))
    LIVE_SOFT = ("live_soft", LiveSoftStrategy(today=True))
    TOMORROW_AT_6 = ("tomorrow_at_6", At6Strategy(today=False))
    TOMORROW_LIVE = ("tomorrow_live", LiveStrategy(today=False))
    TOMORROW_LIVE_SOFT = ("tomorrow_live_soft", LiveSoftStrategy(today=False))
    EXTRAP_PATTERN = ("extrapolated_live_pattern", ExtrapPatternStrategy())
    EXTRAP_PROPORTIONAL = (
        "extrapolated_live_proportional",
        ExtrapProportionalStrategy(),
    )
    EXTRAP_BAND = ("extrapolated_live_band", ExtrapBandStrategy())
    EXTRAP_BAND_RECENT = (
        "extrapolated_live_band_recent",
        ExtrapBandRecentStrategy(),
    )
    EXTRAP_PROPORTIONAL_CAPPED = (
        "extrapolated_live_proportional_capped",
        ExtrapProportionalCappedStrategy(),
    )
    EXTRAP_BLEND = ("extrapolated_live_blend", ExtrapBlendStrategy())

    def __init__(self, key: str, strategy: ForecastStrategy) -> None:
        self.key = key
        self.strategy = strategy

    @property
    def is_today(self) -> bool:
        return self.strategy.is_today

    @property
    def is_tomorrow(self) -> bool:
        return not self.strategy.is_today

    @property
    def is_extrap(self) -> bool:
        return self.strategy.is_extrap

    @property
    def pretty_label(self) -> str:
        """Display label — delegated to bound strategy."""
        return self.strategy.pretty_label

    @property
    def result(self) -> PvForecastResult | None:
        """Current forecast result — from bound strategy."""
        return self.strategy.result

    @classmethod
    def today(cls) -> tuple[PvForecast, ...]:
        """Today-axis variants (AT_6 + LIVE + LIVE_SOFT + 6× EXTRAP)."""
        return tuple(v for v in cls if v.is_today)

    @classmethod
    def tomorrow(cls) -> tuple[PvForecast, ...]:
        """Tomorrow-axis variants (TOMORROW_AT_6 + TOMORROW_LIVE + _SOFT)."""
        return tuple(v for v in cls if v.is_tomorrow)

    @classmethod
    def extrap(cls) -> tuple[PvForecast, ...]:
        """EXTRAP variants (6× extrapolated-from-LIVE)."""
        return tuple(v for v in cls if v.is_extrap)
