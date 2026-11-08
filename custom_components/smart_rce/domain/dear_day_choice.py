"""Dear workday: keep the battery full, or run the day as a normal one.

A dear day (`cheap_hours.is_dear_day`: no hour from 07:00 to T2 below the
export bar) charges the night to 100 and sells nothing in the morning
(`MorningChargeFloor.DEAR_DAY_CAP`, user's decision 28.10.2026): the sun then
goes to the grid at a good price all day. When the morning is much dearer than
the hours the sun refills the battery in, the normal day pays better: the
night charges to the T1 reserve only and the automatic morning discharge sells
what the battery holds above it (09.11.2026: 07:00 at 2355 gross, the refill
hours 10-12 at ~970; user's decision 08.11.2026).

Two variants, valued hour by hour from the night charge's start until T2:

- `FULL` — the night charges to 100, no morning sale, the battery covers T1
  and the sun's surplus goes to the grid;
- `NORMAL` — the night charges to the T1 reserve, the morning plan sells down
  to its target in its hours, the battery covers T1, the sun refills it from
  the charge start and the rest goes to the grid.

The night charge buys only up to its target: a battery already above it stays
as it is. No variant charges at night to sell in the morning (ADR-028 pt 3;
the user ruled it out again 08.11.2026). Before T2 the afternoon charge
(CHARGE_AFTERNOON, grid, T3) tops the battery up to full in both, so the
evening is the same and is left out. Per hour the grid balance is worth RCE
(gross) when exported — the deposit — and the zone's purchase price when
bought: active energy from the deposit, distribution in cash (as
`DayOffChargeDecision`). `NORMAL` wins only `MIN_ADVANTAGE_ZL` better: the
forecast of the sun and the house has its error.

Pure and kept outside `battery_schedule` (`live_reload` order).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from .charge_curve import kwh_to_full
from .target_soc import MIN_SOC_PERCENT

if TYPE_CHECKING:
    from collections.abc import Sequence

# Battery power either way, kWh per hour (`day_off_charge.MAX_KWH_PER_H`).
MAX_KWH_PER_H: Final[float] = 5.0
# Round trip of a kWh through the battery, charged on the way out.
ROUND_TRIP: Final[float] = 0.9
# The afternoon top-up runs in the hours right before T2 (13:00-15:55 in winter).
TOP_UP_HOURS: Final[int] = 3
# User's decision 08.11.2026.
MIN_ADVANTAGE_ZL: Final[float] = 1.0
FULL_SOC: Final[int] = 100


class DearDayVariant(StrEnum):
    FULL = "full"
    NORMAL = "normal"


@dataclass(frozen=True)
class DearDayChoice:
    """Both variants of one dear workday, valued, and the one to run."""

    day: date
    soc: float
    full: VariantValue
    normal: VariantValue
    chosen: DearDayVariant

    @classmethod
    def decide(  # noqa: PLR0913 — the day's inputs
        cls,
        day: date,
        hours: Sequence[DearDayHour],
        *,
        soc: float,
        night_start_hour: int,
        night_end_hour: int,
        reserve: int,
        s0_hour: int,
        t2_hour: int,
        sale_hours: Sequence[int] = (),
        sale_target: int | None = None,
    ) -> DearDayChoice:
        """Value both variants from `soc` (the battery before the night charge)."""
        day_ = _Day(hours, night_start_hour, night_end_hour, s0_hour, t2_hour)
        full = day_.value(DearDayVariant.FULL, soc, FULL_SOC, (), None)
        normal = day_.value(
            DearDayVariant.NORMAL, soc, reserve, tuple(sale_hours), sale_target
        )
        chosen = (
            DearDayVariant.NORMAL
            if normal.value_zl - full.value_zl >= MIN_ADVANTAGE_ZL
            else DearDayVariant.FULL
        )
        return cls(day=day, soc=soc, full=full, normal=normal, chosen=chosen)

    @property
    def advantage_zl(self) -> float:
        """How much better NORMAL is than FULL (negative: worse)."""
        return round(self.normal.value_zl - self.full.value_zl, 2)

    def with_chosen(self, chosen: DearDayVariant) -> DearDayChoice:
        """Return the same valuation with the variant latched earlier."""
        return DearDayChoice(
            day=self.day,
            soc=self.soc,
            full=self.full,
            normal=self.normal,
            chosen=chosen,
        )

    def describe(self) -> str:
        """One line (Polish, the dashboard reads it)."""
        return (
            f"drogi dzień: pełna bateria {self.full.value_zl:+.2f} zł, "
            f"zwykły dzień {self.normal.value_zl:+.2f} zł "
            f"(różnica {self.advantage_zl:+.2f}, próg {MIN_ADVANTAGE_ZL:.2f}, "
            f"SOC {self.soc:.0f}%)"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "soc": round(self.soc, 1),
            "chosen": self.chosen.value,
            "advantage_zl": self.advantage_zl,
            "min_advantage_zl": MIN_ADVANTAGE_ZL,
            "variants": [self.full.to_dict(), self.normal.to_dict()],
        }


@dataclass(frozen=True)
class DearDayHour:
    """One hour of the day (kWh; zl/kWh gross)."""

    surplus_kwh: float
    deficit_kwh: float
    rce_gross: float
    buy_energy: float
    buy_distribution: float


class _Day:
    """The hour-by-hour simulation shared by both variants."""

    def __init__(
        self,
        hours: Sequence[DearDayHour],
        night_start_hour: int,
        night_end_hour: int,
        s0_hour: int,
        t2_hour: int,
    ) -> None:
        self._hours = hours
        self._night = range(night_start_hour, night_end_hour)
        self._first = night_start_hour
        self._s0 = s0_hour
        self._t2 = t2_hour
        # Stored energy above the floor when full (charge-side kWh).
        self._capacity = kwh_to_full(MIN_SOC_PERCENT)

    def value(
        self,
        variant: DearDayVariant,
        soc: float,
        night_target: int,
        sale_hours: tuple[int, ...],
        sale_target: int | None,
    ) -> VariantValue:
        level = self._level(soc)
        night_level = self._level(night_target)
        sale_level = self._level(sale_target) if sale_target is not None else None
        deposit = cash = bought = sold = 0.0
        for h in range(self._first, self._t2):
            hour = self._hours[h]
            grid = 0.0  # + exported, - bought
            if h in self._night and level < night_level:
                take = min(MAX_KWH_PER_H, night_level - level)
                level += take
                grid -= take
            if sale_level is not None and h in sale_hours and level > sale_level:
                give = min(MAX_KWH_PER_H, level - sale_level)
                level -= give
                grid += give * ROUND_TRIP
                sold += give * ROUND_TRIP
            # From the night charge's end (T1) the house draws the battery, the
            # rest from the grid; before it the hold rule keeps the battery
            # (DoD 0 in either variant) and the house buys at T3.
            drawn = (
                min(hour.deficit_kwh / ROUND_TRIP, level, MAX_KWH_PER_H)
                if h >= self._night.stop
                else 0.0
            )
            level -= drawn
            grid -= hour.deficit_kwh - drawn * ROUND_TRIP
            surplus = hour.surplus_kwh
            if h >= self._s0:
                take = min(surplus, MAX_KWH_PER_H, self._capacity - level)
                level += take
                surplus -= take
            grid += surplus
            if h >= self._t2 - TOP_UP_HOURS:
                # DELAYED_TO_END: as late as the remaining hours allow.
                left = self._t2 - h - 1
                take = max(0.0, self._capacity - level - MAX_KWH_PER_H * left)
                take = min(take, MAX_KWH_PER_H)
                level += take
                grid -= take
                bought += take
            if grid > 0:
                deposit += grid * hour.rce_gross
            else:
                deposit += grid * hour.buy_energy
                cash += grid * hour.buy_distribution
        return VariantValue(
            key=variant,
            night_target=night_target,
            value_zl=round(deposit + cash, 2),
            deposit_zl=round(deposit, 2),
            cash_zl=round(cash, 2),
            sold_kwh=round(sold, 2),
            top_up_kwh=round(bought, 2),
        )

    def _level(self, soc: float) -> float:
        """Return the stored energy above the floor at `soc` (charge-side kWh)."""
        return max(0.0, self._capacity - kwh_to_full(soc))


@dataclass(frozen=True)
class VariantValue:
    """One variant's day until T2: value against the grid, deposit / cash."""

    key: DearDayVariant
    night_target: int
    value_zl: float
    deposit_zl: float
    cash_zl: float
    sold_kwh: float
    top_up_kwh: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key.value,
            "night_target": self.night_target,
            "value_zl": self.value_zl,
            "deposit_zl": self.deposit_zl,
            "cash_zl": self.cash_zl,
            "sold_kwh": self.sold_kwh,
            "top_up_kwh": self.top_up_kwh,
        }
