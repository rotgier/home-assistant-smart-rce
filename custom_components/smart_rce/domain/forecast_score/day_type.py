"""Day type — cloudy or sunny, so rankings can be read per kind of day.

Kept as numbers, labelled on reading: the threshold is still being tuned and
must not need 30 days rescored.

- `live_over_p50`: Live's weather multiplier at 06:11, over 07:00-13:00 (the
  ratio of the sums, i.e. per-slot ratios weighted by p50). Known BEFORE the
  fact, so it can also steer a weather-dependent alpha later.
- `actual_pv_over_p50`: what came over 07:00-13:00 against that p50.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from ..bucket import PV_WINDOW_HOURS
from ..pv_forecast import PvForecast
from .day_actuals import PV
from .start_score import forecast_half_hours
from .variant_results import RAW_SOLCAST

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..pv_forecast import PvForecastResult
    from .day_actuals import DayActuals
    from .variant_results import ScoredSnapshot

# Below this Live/p50 multiplier at 06:11 a day counts as cloudy (decision 15.10.2026).
CLOUDY_BELOW: Final = 0.8
_WINDOW_SLOTS: Final = range(PV_WINDOW_HOURS.start * 2, PV_WINDOW_HOURS.stop * 2)


class DayKind(StrEnum):
    CLOUDY = "cloudy"
    SUNNY = "sunny"


def day_type_figures(
    actuals: DayActuals, main_snapshot: ScoredSnapshot | None
) -> DayTypeFigures:
    """Compute the two ratios from the 06:11 snapshot and what came."""
    if main_snapshot is None:
        return DayTypeFigures(None, None)
    p50 = _window_sum(main_snapshot.result(RAW_SOLCAST), actuals.day)
    live = _window_sum(main_snapshot.result(PvForecast.LIVE.key), actuals.day)
    half = actuals.half_hours(PV)
    actual = sum(half.get(slot, 0.0) for slot in _WINDOW_SLOTS)
    if not p50:
        return DayTypeFigures(None, None)
    return DayTypeFigures(
        live_over_p50=round(live / p50, 3) if live is not None else None,
        actual_pv_over_p50=round(actual / p50, 3),
    )


def _window_sum(result: PvForecastResult | None, day: date) -> float | None:
    if result is None:
        return None
    slots = forecast_half_hours(result, day)
    return sum(slots.get(slot, 0.0) for slot in _WINDOW_SLOTS)


@dataclass(frozen=True)
class DayTypeFigures:
    """Ratios behind the day type (None = no 06:11 forecast)."""

    live_over_p50: float | None
    actual_pv_over_p50: float | None

    @property
    def kind(self) -> DayKind | None:
        if self.live_over_p50 is None:
            return None
        return DayKind.CLOUDY if self.live_over_p50 < CLOUDY_BELOW else DayKind.SUNNY

    def to_dict(self) -> dict[str, Any]:
        return {
            "live_over_p50": self.live_over_p50,
            "actual_pv_over_p50": self.actual_pv_over_p50,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DayTypeFigures:
        return cls(data.get("live_over_p50"), data.get("actual_pv_over_p50"))
