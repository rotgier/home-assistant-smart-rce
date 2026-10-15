"""ForecastScoreStatistics — final daily figures as external long-term statistics.

One statistic per headline figure (`smart_rce:forecast_score_<key>`), one row
per day: start = the day's local midnight, mean = min = max = state = the
FINAL value. A clean daily series, unlike a sensor (which would blend
yesterday's final with today's 16:06 preliminary in its hourly means). Rows can
be written for any past day, and the 00:20 check simply rewrites its day's row.
Dashboards read them with `statistics-graph`, `period: day`.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import TYPE_CHECKING, Final

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import callback

from ..domain.forecast_score.headlines import HEADLINES

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import tzinfo

    from homeassistant.core import HomeAssistant

    from ..domain.forecast_score.day_score import DayScore

SOURCE: Final = "smart_rce"


def statistic_id(key: str) -> str:
    return f"{SOURCE}:forecast_score_{key}"


class ForecastScoreStatistics:
    """Writes final days' headline figures as daily external statistics."""

    def __init__(self, hass: HomeAssistant, tz: Callable[[], tzinfo]) -> None:
        self._hass = hass
        self._tz = tz

    @callback
    def publish(self, scores: Iterable[DayScore]) -> None:
        """Write (or rewrite) the rows of `scores` — final days only."""
        days = list(scores)
        tz = self._tz()
        for headline in HEADLINES:
            rows = [
                StatisticData(
                    start=datetime.combine(score.day, time(0), tz),
                    state=float(value),
                    mean=float(value),
                    min=float(value),
                    max=float(value),
                )
                for score in days
                if (value := headline.value(score)) is not None
            ]
            if not rows:
                continue
            async_add_external_statistics(
                self._hass,
                StatisticMetaData(
                    source=SOURCE,
                    statistic_id=statistic_id(headline.key),
                    name=f"EMS Forecast Score {headline.name} (daily)",
                    unit_of_measurement=headline.unit,
                    unit_class=None,
                    mean_type=StatisticMeanType.ARITHMETIC,
                    has_sum=False,
                ),
                rows,
            )
