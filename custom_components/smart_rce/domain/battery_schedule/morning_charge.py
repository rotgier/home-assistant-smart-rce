"""MorningChargeFloor — the night charge on a day the sun will not fill the battery.

CHARGE_MORNING_AUTO (04:00-07:00, workdays) charges to the T1 reserve. On a
day whose sun cannot refill the battery anyway, the energy the afternoon would
buy back from the grid is bought at night instead — both are T3 in winter, so
only the hour of purchase moves, and the morning T1 is covered (user's decision
22.10.2026: that day the battery hit 10% before 13:00 and T1 bought 1.41 kWh):

    surplus = sum of max(0, PV p90 - consumption p40) per half-hour from 07:00
              to T2, only in hours whose gross RCE is below the export bar
    fill    = 100 - pp_below_full(surplus)   — the lowest SoC the sun refills
    target  = min(90, fill + reserve without margin - 10)

and the slot charges to max(reserve, target). The reserve without margin less
the floor is the dip until the sun covers the house. With `fill` at the floor
the sun refills any level, so no floor binds and the reserve alone stands.

Optimistic on purpose (PV p90, consumption p40): only what even a good day
leaves short is bought. Hours at or above the export bar do not count — the
same bar the planners sell from the battery by: there exporting the PV and
buying back at T3 pays better than storing it. Not `lowest_soc_the_sun_fills`:
that counts the sun in the price window from the charge start only, which on
22.10 (start 06:00) ended at 07:00 and gave 100 by definition.

Backtest 05-22.10 (workdays): home-assistant-ops
`research/2026-10-22-poranne-ladowanie-slaby-dzien/` — at 750 three days
changed (14.10 to 71, 20.10 and 22.10 to 90), -0.42 zl, none lost.

A dear day (no hour from 07:00 to T2 below the bar, `dear_day`) charges to 100
instead (user's decision 28.10.2026): its sun goes to the grid at a good price
anyway, so the battery needs no room left for it, and the house runs T1 on the
night's T3 energy. The morning sale then stays off (`MorningDischargeService`)
— selling at 07:00 what the night just bought is the grid-battery-grid round
trip ADR-028 rules out. 28.10 (every hour above 950 gross) charged to 90 and
would have sold it down to 16% at 07:00.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, ClassVar

from ..charge_curve import pp_below_full
from ..pv_surplus import SurplusProfile, surplus_window_end
from ..target_soc import MIN_SOC_PERCENT

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..rce import RceDayPrices


@dataclass(frozen=True)
class MorningChargeFloor:
    """The level the night charge goes to so the sun can still fill the battery."""

    CAP: ClassVar[int] = 90
    DEAR_DAY_CAP: ClassVar[int] = 100
    FIRST_HOUR: ClassVar[int] = 7

    day: date
    surplus_kwh: float
    fill: int
    dip: int
    # None: the sun refills from the floor, the reserve alone stands.
    target: int | None
    # No hour from 07:00 to T2 below the export bar.
    dear_day: bool = False

    @classmethod
    def decide(
        cls,
        day: date,
        *,
        pv: Mapping[int, float],
        consumption: Mapping[int, float],
        prices: RceDayPrices,
        export_threshold: float,
        reserve_without_margin: int,
    ) -> MorningChargeFloor:
        """Floor for `day` from PV p90 and consumption p40 per 30-min slot (kWh)."""
        profile = SurplusProfile.build(day, pv, consumption)
        cheap = [
            hour
            for hour in range(cls.FIRST_HOUR, surplus_window_end(day))
            if prices.gross_at(hour) < export_threshold
        ]
        surplus = sum(profile.total(hour * 60, (hour + 1) * 60) for hour in cheap)
        fill = round(100 - pp_below_full(surplus))
        dip = max(0, reserve_without_margin - MIN_SOC_PERCENT)
        dear_day = not cheap
        cap = cls.DEAR_DAY_CAP if dear_day else cls.CAP
        return cls(
            day=day,
            surplus_kwh=round(surplus, 2),
            fill=fill,
            dip=dip,
            target=min(cap, fill + dip) if fill > MIN_SOC_PERCENT else None,
            dear_day=dear_day,
        )

    def lifts(self, reserve: int) -> int:
        """Target of the night charge: the reserve, or this floor when higher."""
        return reserve if self.target is None else max(reserve, self.target)

    def describe(self) -> str:
        """One line for the slot's target source."""
        floor = "none" if self.target is None else str(self.target)
        dear = ", dear day" if self.dear_day else ""
        return (
            f"weak-sun floor {floor} (fill {self.fill}, dip {self.dip}, "
            f"surplus {self.surplus_kwh:.2f} kWh{dear})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "surplus_kwh": self.surplus_kwh,
            "fill": self.fill,
            "dip": self.dip,
            "target": self.target,
            "dear_day": self.dear_day,
        }
