"""T3PeakPlan — sell the battery in the afternoon T3 peak, buy it back before T2.

A winter workday's afternoon (13:00-16:00) is T3: buying from the grid costs
~0.63 zl gross. When RCE peaks there — 10.11.2026: 13:00 3.89, 14:00 4.50 zl
gross, every T2 hour 16-20 below — the battery sells in the peak hour and the
afternoon charge buys the energy back before T2. A grid-battery-grid round
trip that ADR-028 pt 3 rules out in general; the user made this exception on
10.11.2026 (pt 3a).

Two variants, simulated minute by minute from the battery level at 13:00
(the user's decision 10.11.2026):

- `13` — sell 13:00-14:00 (down to the floor), charge 14:00 until the
  afternoon charge's end at full power;
- `14` — charge 13:00-14:00 at full power, sell 14:00-15:00 no lower than the
  level from which the charge 15:00 until the afternoon charge's end still
  reaches `END_SOC`, then charge again.

A variant takes part only when its sale hour pays more than every T2 hour (the
evening sells otherwise, along its ladder) and only when it beats the normal
afternoon — CHARGE_AFTERNOON DELAYED_TO_END to full by its end — by
`MIN_GAIN_ZL` after the battery's wear (`CYCLE_COST_PER_KWH` per kWh it gives).
What the variant leaves the battery short at T2 against the normal afternoon
is the evening's sale (or the house's T2 purchase) moved forward: valued at
the dearer of the dearest T2 hour and the T2 purchase price. Per hour the grid
balance is worth RCE gross when exported and the zone's purchase price when
bought (energy from the deposit, distribution in cash): hourly settlement, so a
charge in an hour the sun still exports in costs that hour's RCE.

Pace from the schedule's own rate model (`Direction`), energy per pp from the
charge curve. The house draws the battery while a sale runs (DoD 90) and the
grid otherwise (HOLD_FOR_T2, DoD 0).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from typing import TYPE_CHECKING, Any, ClassVar, Final

from ..charge_curve import kwh_per_pp
from .direction import Direction

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# Round trip of a kWh through the battery, charged on the way out.
_ROUND_TRIP: Final[float] = 0.9
# A DELAYED_TO_END start leads the rate model by this much (`_DELAYED_LEAD_SEC`).
_LEAD_SEC: Final[float] = 240.0
_FIRST_HOUR: Final[int] = 13
_FULL: Final[float] = 100.0


@dataclass(frozen=True)
class T3PeakHour:
    """One hour of the day (kWh per hour; zl/kWh gross)."""

    surplus_kwh: float
    deficit_kwh: float
    rce_gross: float
    buy_energy: float
    buy_distribution: float


@dataclass(frozen=True)
class T3PeakPlan:
    """The afternoon sale the schedule runs today."""

    MIN_GAIN_ZL: ClassVar[float] = 3.0
    CYCLE_COST_PER_KWH: ClassVar[float] = 0.25
    # The 14:00 variant still reaches this before T2 (user, 10.11.2026).
    END_SOC: ClassVar[int] = 80
    FLOOR: ClassVar[int] = 10
    SELL_HOURS: ClassVar[tuple[int, ...]] = (13, 14)

    day: date
    sell_hour: int
    target: int
    # The 14:00 variant charges 13:00-14:00 first.
    pre_charge: bool
    gain_zl: float

    @property
    def start(self) -> time:
        return time(self.sell_hour, 0)

    @property
    def end(self) -> time:
        return time(self.sell_hour + 1, 0)

    @property
    def charge_from(self) -> time:
        """Where the afternoon charge starts again after the sale."""
        return time(self.sell_hour + 1, 0)

    def describe(self) -> str:
        """One-line source for the slot telegram."""
        pre = "charge 13-14, " if self.pre_charge else ""
        return (
            f"T3 peak: {pre}sell {self.sell_hour:02d}-{self.sell_hour + 1:02d} "
            f"to {self.target}%, gain {self.gain_zl:+.2f} zl"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "sell_hour": self.sell_hour,
            "target": self.target,
            "pre_charge": self.pre_charge,
            "gain_zl": self.gain_zl,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> T3PeakPlan:
        return cls(
            day=date.fromisoformat(data["day"]),
            sell_hour=int(data["sell_hour"]),
            target=int(data["target"]),
            pre_charge=bool(data["pre_charge"]),
            gain_zl=float(data["gain_zl"]),
        )


@dataclass(frozen=True)
class T3PeakEvaluation:
    """Both variants of one afternoon, valued, and the plan (None: no sale)."""

    day: date
    soc: float
    variants: tuple[T3PeakVariant, ...]
    plan: T3PeakPlan | None

    @classmethod
    def evaluate(  # noqa: PLR0913 — the afternoon's inputs, one call site + tests
        cls,
        day: date,
        hours: Sequence[T3PeakHour],
        *,
        soc: float,
        t2_start: int,
        charge_end_minute: int,
        t2_hours: Sequence[int],
        t2_buy: float,
    ) -> T3PeakEvaluation:
        """Value both variants against the normal afternoon from `soc` at 13:00."""
        sim = _Afternoon(hours, t2_start, charge_end_minute)
        baseline = sim.run(soc, sim.normal)
        dearest_t2 = max(hours[h].rce_gross for h in t2_hours)
        moved_value = max(dearest_t2, t2_buy)
        variants = []
        for hour in T3PeakPlan.SELL_HOURS:
            pre_charge = hour > _FIRST_HOUR
            target = (
                sim.floor_reaching(T3PeakPlan.END_SOC, (hour + 1) * 60)
                if pre_charge
                else T3PeakPlan.FLOOR
            )
            run = sim.run(soc, sim.arbitrage(hour, target, pre_charge=pre_charge))
            short = max(0.0, baseline.end_soc - run.end_soc)
            short_kwh = _kwh_between(run.end_soc, run.end_soc + short) * _ROUND_TRIP
            wear = run.given_kwh * T3PeakPlan.CYCLE_COST_PER_KWH
            gain = run.value_zl - baseline.value_zl - short_kwh * moved_value - wear
            dearer = hours[hour].rce_gross > dearest_t2
            variants.append(
                T3PeakVariant(
                    sell_hour=hour,
                    target=target,
                    pre_charge=pre_charge,
                    gain_zl=round(gain, 2),
                    end_soc=round(run.end_soc, 1),
                    given_kwh=round(run.given_kwh, 2),
                    bought_kwh=round(run.bought_kwh - baseline.bought_kwh, 2),
                    dearer_than_t2=dearer,
                    eligible=dearer and gain >= T3PeakPlan.MIN_GAIN_ZL,
                )
            )
        best = max(
            (v for v in variants if v.eligible), key=lambda v: v.gain_zl, default=None
        )
        plan = (
            T3PeakPlan(
                day=day,
                sell_hour=best.sell_hour,
                target=best.target,
                pre_charge=best.pre_charge,
                gain_zl=best.gain_zl,
            )
            if best is not None
            else None
        )
        return cls(day=day, soc=soc, variants=tuple(variants), plan=plan)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "soc": round(self.soc, 1),
            "plan": self.plan.to_dict() if self.plan is not None else None,
            "min_gain_zl": T3PeakPlan.MIN_GAIN_ZL,
            "cycle_cost_per_kwh": T3PeakPlan.CYCLE_COST_PER_KWH,
            "variants": [v.to_dict() for v in self.variants],
        }


@dataclass(frozen=True)
class T3PeakVariant:
    """One variant's afternoon against the normal one."""

    sell_hour: int
    target: int
    pre_charge: bool
    gain_zl: float
    end_soc: float
    given_kwh: float
    bought_kwh: float
    dearer_than_t2: bool
    eligible: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "sell_hour": self.sell_hour,
            "target": self.target,
            "pre_charge": self.pre_charge,
            "gain_zl": self.gain_zl,
            "end_soc": self.end_soc,
            "given_kwh": self.given_kwh,
            "extra_bought_kwh": self.bought_kwh,
            "dearer_than_t2": self.dearer_than_t2,
            "eligible": self.eligible,
        }


@dataclass(frozen=True)
class _Run:
    value_zl: float
    end_soc: float
    given_kwh: float
    bought_kwh: float


# Minute of the day, SoC -> "charge" / "sell" / "idle"; sell targets come with it.
_Mode = tuple[str, float]


class _Afternoon:
    """Minute-by-minute simulation of 13:00 until T2."""

    def __init__(
        self, hours: Sequence[T3PeakHour], t2_start: int, charge_end_minute: int
    ) -> None:
        self._hours = hours
        self._t2 = t2_start
        self._charge_end = charge_end_minute

    def normal(self, minute: int, soc: float) -> _Mode:
        """CHARGE_AFTERNOON DELAYED_TO_END: start when the time left just suffices."""
        if minute >= self._charge_end:
            return "idle", 0.0
        need = Direction.CHARGE.seconds_for_soc_traversal(soc, _FULL)
        left = (self._charge_end - minute) * 60
        return ("charge", 0.0) if left <= need + _LEAD_SEC else ("idle", 0.0)

    def arbitrage(
        self, hour: int, target: int, *, pre_charge: bool
    ) -> Callable[[int, float], _Mode]:
        def mode(minute: int, soc: float) -> _Mode:
            if minute < hour * 60:
                return ("charge", 0.0) if pre_charge else ("idle", 0.0)
            if minute < (hour + 1) * 60:
                return "sell", float(target)
            return ("charge", 0.0) if minute < self._charge_end else ("idle", 0.0)

        return mode

    def floor_reaching(self, end_soc: int, from_minute: int) -> int:
        """Lowest SoC from which a charge from `from_minute` reaches `end_soc` in time."""
        budget = (self._charge_end - from_minute) * 60 - _LEAD_SEC
        for soc in range(T3PeakPlan.FLOOR, end_soc):
            if Direction.CHARGE.seconds_for_soc_traversal(soc, end_soc) <= budget:
                return soc
        return end_soc

    def run(self, soc: float, plan: Callable[[int, float], _Mode]) -> _Run:
        grid = dict.fromkeys(range(_FIRST_HOUR, self._t2), 0.0)
        given = bought = 0.0
        for minute in range(_FIRST_HOUR * 60, self._t2 * 60):
            hour = self._hours[minute // 60]
            house = (hour.surplus_kwh - hour.deficit_kwh) / 60
            mode, target = plan(minute, soc)
            if mode == "sell" and soc > target:
                step = min(60 / _sec_per_pp(Direction.DISCHARGE, soc), soc - target)
                out = step * kwh_per_pp(soc) * _ROUND_TRIP
                grid[minute // 60] += out + house
                given += out
                soc -= step
            elif mode == "charge" and soc < _FULL:
                step = min(60 / _sec_per_pp(Direction.CHARGE, soc), _FULL - soc)
                taken = step * kwh_per_pp(soc)
                grid[minute // 60] += house - taken
                bought += taken
                soc += step
            else:
                grid[minute // 60] += house
        value = 0.0
        for h, kwh in grid.items():
            hour = self._hours[h]
            value += (
                kwh * hour.rce_gross
                if kwh > 0
                else kwh * (hour.buy_energy + hour.buy_distribution)
            )
        return _Run(value_zl=value, end_soc=soc, given_kwh=given, bought_kwh=bought)


def _sec_per_pp(direction: Direction, soc: float) -> float:
    return next(
        z.sec_per_pp for z in direction.rate_zones if z.soc_from <= soc < z.soc_to
    )


def _kwh_between(low: float, high: float) -> float:
    """Charge-side kWh from `low` to `high` SoC, pp by pp."""
    total, soc = 0.0, low
    while soc < high:
        step = min(1.0, high - soc)
        total += step * kwh_per_pp(soc)
        soc += step
    return total
