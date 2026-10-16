"""MorningDischargePlan — what the automatic morning discharge does today.

Workdays only. The slot starts at the dearest whole hour from 07:00 until the
earlier of its window end (10:00) and S0 (the charge start from RCE prices),
then moves on hour by hour until S0, each hour DELAYED_TO_END: the battery
stands on the target when the hour closes (settlement is hourly, so when inside
the hour it gave the energy away does not matter). An hour takes part only when
it is dearer than every hour the sun charges the battery in afterwards — from
S0 until the battery is forecast full (`full_charge_minute`), or until T2 when
it is not: the energy it sells comes back from PV in those hours, and the
surplus after the battery is full is exported whatever the morning did
(17.10.2026: full ~13:20, so 09 at 705 sells although 15:00 pays 715). S0,
not a forecast-moved start: an hour dearer than the whole PV window is worth
selling even when the battery already takes PV.

Target, recomputed every minute (agreed with the user 15.10.2026):
- the sun fills the battery even from the T1 reserve -> the reserve, at any
  price: the energy makes room for the sun and sells dearer than at noon;
- otherwise, below the export bar -> the lowest SoC the sun still fills;
- otherwise -> down to ARBITRAGE_PP above the reserve, but no lower than the
  lowest SoC the sun still fills (target = min(reserve + ARBITRAGE_PP, fill)):
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
    """Price at or above the bar: ARBITRAGE_PP above the reserve, capped at fill."""


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
    hours: tuple[int, ...] = ()
    # Minute of the day the battery is forecast full from S0 (None: not by T2)
    # — what `hours` compared against; kept for scoring the forecast.
    full_at_minute: int | None = None

    @classmethod
    def decide(
        cls,
        *,
        day: date,
        hour: int,
        gross_price: float,
        reserve: int,
        fill: int,
        hours: tuple[int, ...] = (),
        full_at_minute: int | None = None,
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
            hours=hours,
            full_at_minute=full_at_minute,
        )

    @classmethod
    def best_hour(cls, prices: RceDayPrices, first: int, last: int) -> int | None:
        """Dearest whole hour h with max(first, 07) <= h and h + 1 <= last."""
        hours = range(max(first, cls.EARLIEST_HOUR), last)
        return max(hours, key=prices.gross_at, default=None)

    @classmethod
    def hours_from(
        cls, prices: RceDayPrices, best: int, s0_hour: int, refill_end_hour: int
    ) -> tuple[int, ...]:
        """Hours from the dearest one until S0 that pay off (see the module doc).

        `refill_end_hour`: first hour the sun no longer charges the battery in —
        the hour after the forecast full charge, or T2.
        """
        return tuple(
            hour
            for hour in range(best, s0_hour)
            if all(
                prices.gross_at(hour) > prices.gross_at(later)
                for later in range(max(hour + 1, s0_hour), refill_end_hour)
            )
        )

    @property
    def start(self) -> time:
        return time(self.hour, 0)

    @property
    def end(self) -> time:
        return time(self.hour + 1, 0)

    def describe(self) -> str:
        """One-line source for the slot telegram: branch, hour, gross price."""
        return (
            f"{self.branch.value} {self.hour:02d}-{self.hour + 1:02d}, "
            f"{self.gross_price:.0f} brutto, reserve {self.reserve}, sun fills {self.fill}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "window": f"{self.hour:02d}:00-{self.hour + 1:02d}:00",
            "target": self.target,
            "reserve": self.reserve,
            "fill": self.fill,
            "gross_price": round(self.gross_price, 1),
            "branch": self.branch.value,
            "hours": [f"{h:02d}:00" for h in self.hours],
            "full_at": (
                f"{self.full_at_minute // 60:02d}:{self.full_at_minute % 60:02d}"
                if self.full_at_minute is not None
                else None
            ),
        }
