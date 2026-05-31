"""PvStrategy enum + LivePvSignals VO — small leaf module to break circularity.

PvForecastCatalog imports VOs (AdjustedPvForecast, SolcastPeriod, ...) from
`pv_forecast`. PvForecast (TargetSoc aggregate) needs PvStrategy enum to
read from catalog. Putting the enum here avoids the circular import.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class PvStrategy(StrEnum):
    """All PV forecast scenarios served by `PvForecastCatalog`.

    Naming convention: `<date_axis>_<source>` where source ∈ {at_6, live,
    extrap_*}. Today's variants drop the date prefix (implicit).
    """

    AT_6 = "at_6"
    LIVE = "live"
    TOMORROW_AT_6 = "tomorrow_at_6"
    TOMORROW_LIVE = "tomorrow_live"
    EXTRAP_PATTERN = "extrapolated_live_pattern"
    EXTRAP_PROPORTIONAL = "extrapolated_live_proportional"
    EXTRAP_BAND = "extrapolated_live_band"
    EXTRAP_BAND_RECENT = "extrapolated_live_band_recent"


TODAY_STRATEGIES: Final[tuple[PvStrategy, ...]] = (
    PvStrategy.AT_6,
    PvStrategy.LIVE,
    PvStrategy.EXTRAP_PATTERN,
    PvStrategy.EXTRAP_PROPORTIONAL,
    PvStrategy.EXTRAP_BAND,
    PvStrategy.EXTRAP_BAND_RECENT,
)

TOMORROW_STRATEGIES: Final[tuple[PvStrategy, ...]] = (
    PvStrategy.TOMORROW_AT_6,
    PvStrategy.TOMORROW_LIVE,
)

EXTRAP_STRATEGIES: Final[tuple[PvStrategy, ...]] = (
    PvStrategy.EXTRAP_PATTERN,
    PvStrategy.EXTRAP_PROPORTIONAL,
    PvStrategy.EXTRAP_BAND,
    PvStrategy.EXTRAP_BAND_RECENT,
)


@dataclass(frozen=True)
class LivePvSignals:
    """PV-side live readings snapshot — single VO passed to catalog per tick.

    Replaces 4 separate field writes on the aggregate from application
    service. Service builds via `LiveRateReader` once per tick, hands to
    `catalog.refresh_live_signals(signals)`.
    """

    pv_power_w: float | None = None
    bucket_so_far_kwh: float | None = None
    derivative_w_per_min: float | None = None
    stability_stable: bool | None = None
