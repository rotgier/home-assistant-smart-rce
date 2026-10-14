"""MorningDischargePlan — what the automatic morning discharge does today.

Workdays only. The slot covers the dearest whole hour from 07:00 until the
earlier of its window end (10:00) and the charge start, and discharges
DELAYED_TO_END: the battery stands on the target when that hour closes
(settlement is hourly, so when inside the hour it gave the energy away does not
matter). Before 07:00 the battery is charged for the dear hours, from the charge
start it takes PV.

Target, recomputed every minute (agreed with the user 15.10.2026):
- the sun fills the battery even from the T1 reserve -> the reserve, at any
  price: the energy makes room for the sun and sells dearer than at noon;
- otherwise, below the export bar -> the lowest SoC the sun still fills;
- otherwise -> up to ARBITRAGE_PP below that level, never below the reserve:
  the afternoon charge buys the rest back in T3 (626 x 1.1 ~ 690 gross break
  even, the bar is 750).
Backtest: home-assistant-ops research/2026-10-15-poranne-rozladowanie/.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from ..rce import RceDayPrices


class MorningDischargeBranch(StrEnum):
    """Which part of the rule set the target."""

    SUN_ENOUGH = "sun_enough"
    """The sun fills the battery even from the reserve: target = reserve."""

    ROOM = "room"
    """Price below the bar: only the room the sun refills."""

    ARBITRAGE = "arbitrage"
    """Price at or above the bar: up to ARBITRAGE_PP below what the sun refills."""


@dataclass(frozen=True)
class MorningDischargePlan:
    """Today's automatic morning discharge: the hour and the SoC to stand on."""

    # Same bar as `EveningPlan.EXPORT_THRESHOLD` (gross PLN/MWh) — not imported:
    # evening_plan depends on this package. A test keeps the two equal.
    EXPORT_THRESHOLD: ClassVar[float] = 750.0
    ARBITRAGE_PP: ClassVar[int] = 10
    EARLIEST_HOUR: ClassVar[int] = 7

    day: date
    hour: int
    target: int
    reserve: int
    fill: int
    gross_price: float
    branch: MorningDischargeBranch

    @classmethod
    def decide(
        cls,
        *,
        day: date,
        hour: int,
        gross_price: float,
        reserve: int,
        fill: int,
    ) -> MorningDischargePlan:
        """Pick the target from the reserve and the lowest SoC the sun fills."""
        if fill <= reserve:
            target, branch = reserve, MorningDischargeBranch.SUN_ENOUGH
        elif gross_price < cls.EXPORT_THRESHOLD:
            target, branch = fill, MorningDischargeBranch.ROOM
        else:
            target = min(reserve + cls.ARBITRAGE_PP, fill)
            branch = MorningDischargeBranch.ARBITRAGE
        return cls(
            day=day,
            hour=hour,
            target=target,
            reserve=reserve,
            fill=fill,
            gross_price=gross_price,
            branch=branch,
        )

    @classmethod
    def best_hour(cls, prices: RceDayPrices, first: int, last: int) -> int | None:
        """Dearest whole hour h with max(first, 07) <= h and h + 1 <= last."""
        hours = range(max(first, cls.EARLIEST_HOUR), last)
        return max(hours, key=prices.gross_at, default=None)

    @property
    def start(self) -> time:
        return time(self.hour, 0)

    @property
    def end(self) -> time:
        return time(self.hour + 1, 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "window": f"{self.hour:02d}:00-{self.hour + 1:02d}:00",
            "target": self.target,
            "reserve": self.reserve,
            "fill": self.fill,
            "gross_price": round(self.gross_price, 1),
            "branch": self.branch.value,
        }
