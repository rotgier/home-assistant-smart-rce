"""Headline figures of a scored day — the numbers kept as a daily series.

One list, two readers: the long-term daily statistics (`smart_rce:forecast_score_<key>`,
one row per final day) and the "last final" sensors.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .day_score import DayScore


@dataclass(frozen=True)
class Headline:
    """One figure: key, English name, unit, how to read it off a day."""

    key: str
    name: str
    unit: str
    value: Callable[[DayScore], float | int | None]


HEADLINES: Final[tuple[Headline, ...]] = (
    Headline("reserve_error", "T1 reserve error", "pp", lambda s: s.reserve_error_pp),
    Headline(
        "reserve_error_pv",
        "T1 reserve error PV part",
        "pp",
        lambda s: s.reserve_error_pv_pp,
    ),
    Headline(
        "reserve_error_consumption",
        "T1 reserve error consumption part",
        "pp",
        lambda s: s.reserve_error_consumption_pp,
    ),
    Headline(
        "reserve_line_error",
        "T1 reserve line error 08:41",
        "pp",
        lambda s: s.reserve_line_error_pp,
    ),
    Headline("start_delta", "Charge start error", "h", lambda s: s.start_delta_h),
    Headline("t1_import", "T1 grid import", "kWh", lambda s: s.outcome.t1_import_kwh),
    Headline("min_soc_t1", "T1 minimum SoC", "%", lambda s: s.outcome.min_soc_t1),
    Headline(
        "grid_topup",
        "Afternoon grid top-up",
        "kWh",
        lambda s: s.outcome.grid_topup_kwh,
    ),
    Headline(
        "morning_export",
        "Morning export 07-10",
        "kWh",
        lambda s: s.outcome.morning_export_kwh,
    ),
)
