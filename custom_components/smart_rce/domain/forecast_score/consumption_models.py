"""Consumption models the scoreboard compares — how earlier days become one expectation.

Each model turns earlier days' consumption (half hours, key 0..47) into the
profile a decision would read. `P60` is production's (both decisions since
15.10.2026); the others are scored next to it, with ACTUAL PV, against what
the house really used.

`OLD_RULE` (T1 reserve until 14.10.2026: max of flat and the earlier days
without the worst) is not a profile but a rule over per-day reserves; it is
scored for the reserve only.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Final

from ..bucket import PV_WINDOW_HOURS, Bucket, Buckets
from ..consumption_percentile import CONSUMPTION_PERCENTILE, percentile_by_key
from ..consumption_profiles import ConsumptionProfile
from ..target_soc import CONSUMPTION_PER_30MIN

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class ConsumptionModel(Enum):
    """Value: (key, percentile of earlier days or None)."""

    FLAT = ("flat", None)
    P50 = ("p50", 0.5)
    P60 = ("p60", 0.6)
    P70 = ("p70", 0.7)
    P80 = ("p80", 0.8)
    P90 = ("p90", 0.9)
    OLD_RULE = ("old_rule", None)

    def __init__(self, key: str, fraction: float | None) -> None:
        self.key = key
        self.fraction = fraction

    @property
    def is_profile(self) -> bool:
        """A per-slot profile (everything but the old max-of rule)."""
        return self is not ConsumptionModel.OLD_RULE

    def half_hours(
        self, earlier: Sequence[Mapping[int, float]]
    ) -> dict[int, float] | None:
        """Return expected consumption per half hour; None = flat 0.9 kWh/h.

        None for a percentile model without earlier days: the decision then
        has nothing to read and the model is not scored.
        """
        if self.fraction is None or not earlier:
            return None
        return percentile_by_key(earlier, self.fraction)

    def reserve_profile(
        self, earlier: Sequence[Mapping[int, float]]
    ) -> ConsumptionProfile | None:
        """Return the 7:00-12:30 profile the T1 reserve would read (None: none)."""
        if self is ConsumptionModel.FLAT:
            return ConsumptionProfile.flat()
        if self.fraction is None or not earlier:
            return None
        windows = [window_profile(day) for day in earlier]
        if self.fraction == CONSUMPTION_PERCENTILE:
            return ConsumptionProfile.p60_of(windows)
        return ConsumptionProfile(
            buckets=Buckets(
                by_bucket=percentile_by_key(
                    (p.buckets.by_bucket for p in windows), self.fraction
                )
            )
        )


PRODUCTION_MODEL: Final = ConsumptionModel.P60


def window_profile(half_hours: Mapping[int, float]) -> ConsumptionProfile:
    """One earlier day as the reserve reads it — a missing bucket at the flat value.

    The same fill production's loader applies to a day it has data for.
    """
    return ConsumptionProfile(
        buckets=Buckets(
            by_bucket={
                Bucket(h, m): half_hours.get(h * 2 + m // 30, CONSUMPTION_PER_30MIN)
                for h in PV_WINDOW_HOURS
                for m in (0, 30)
            }
        )
    )
