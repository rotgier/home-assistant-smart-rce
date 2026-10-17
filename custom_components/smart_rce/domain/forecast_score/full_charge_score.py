"""D3 — the forecast full charge: when the battery fills from S0, and what it decided.

The morning discharge qualifies an hour against the hours the sun refills the
battery in, from S0 until the forecast FULL charge (ec1a691, 17.10.2026), so
the minute it forecasts decides which dear morning hours are sold. Scored like
the other decisions:

- system: `full_at` of the plan in force just before the first discharge hour
  started (the plan is rebuilt every minute; that one decided);
- optimal: the same `full_charge_minute` on what really came (actual PV and
  consumption), from the same start (S0) and the same SoC (the reserve without
  margin at that moment) — the forecast's error alone;
- actual: the first slot the SoC really reached full (the facts panel) — also
  depends on how the morning really went (discharge depth, the start);
- model: the same `full_charge_minute` on what really came, from the ACTUAL
  charge start with the SoC the battery really had then — against `actual` it
  isolates the charging model (charge curve, pace of `Direction.CHARGE`, the
  BMS plateau).

error = system − optimal (the surplus forecast), model_error = model − actual
(the charging model); minutes, + = computed full later than the reference.
`hours` vs `optimal_hours`: the discharge hours each full charge qualifies —
does the forecast error change what was sold?
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..battery_schedule.morning_discharge import MorningDischargePlan
from ..charge_start_shift import full_charge_minute
from ..pv_surplus import SurplusProfile, surplus_window_end
from ..rce import RceDayPrices
from .day_actuals import CONSUMPTION, PV

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .day_actuals import DayActuals

# The slot's default window (07:00-10:00): the dearest hour is picked in it.
_WINDOW_FIRST_HOUR = 7
_WINDOW_END_HOUR = 10


@dataclass(frozen=True)
class PlanReading:
    """The morning discharge plan as it stood at one moment (from its sensor)."""

    at: datetime
    full_at: int | None  # minute of the day; None = not full before T2
    hours: tuple[int, ...]
    # What the plan charged from: the reserve without margin, the 10% floor
    # on a day off (f4ea9ff).
    start_soc: float | None
    day_off: bool = False


def score_full_charge(  # noqa: PLR0917 — one call site, the day's facts
    actuals: DayActuals,
    plan: PlanReading,
    prices: Sequence[float],
    s0: int,
    actual_full: int | None,
    actual_start: int | None,
) -> FullChargeScore:
    """Score the plan's full charge against the same computation on what came."""
    day = actuals.day
    t2 = surplus_window_end(day) * 60
    day_prices = RceDayPrices(None, day, tuple(prices))
    came = SurplusProfile.build(
        day, actuals.half_hours(PV), actuals.half_hours(CONSUMPTION)
    )
    optimal = (
        full_charge_minute(came, s0, plan.start_soc, t2)
        if plan.start_soc is not None
        else None
    )
    start = actual_start if actual_start is not None else s0
    start_soc = actuals.soc_near(actuals.at(start))
    model = (
        full_charge_minute(came, start, start_soc, t2)
        if start_soc is not None
        else None
    )
    # A day off searches from 05:00, whatever the slot's window (as production).
    first = (
        MorningDischargePlan.EARLIEST_HOUR_DAY_OFF
        if plan.day_off
        else _WINDOW_FIRST_HOUR
    )
    best = MorningDischargePlan.best_hour(
        day_prices, first, min(_WINDOW_END_HOUR, s0 // 60), day_off=plan.day_off
    )
    return FullChargeScore(
        planned_at=plan.at,
        start_soc=plan.start_soc,
        system=plan.full_at,
        optimal=optimal,
        actual=actual_full,
        hours=plan.hours,
        optimal_hours=(
            _hours(day_prices, best, s0, t2, optimal) if best is not None else ()
        ),
        hours_recomputed=(
            _hours(day_prices, best, s0, t2, plan.full_at) if best is not None else ()
        ),
        s0_soc=actuals.soc_near(actuals.at(s0)),
        actual_start=start,
        actual_start_soc=start_soc,
        model=model,
    )


def _hours(
    prices: RceDayPrices, best: int, s0: int, t2: int, full: int | None
) -> tuple[int, ...]:
    """Discharge hours a full charge at `full` qualifies (as production rounds it)."""
    refill_end = t2 // 60 if full is None else min(t2 // 60, -(-full // 60))
    return MorningDischargePlan.hours_from(prices, best, s0 // 60, refill_end)


@dataclass(frozen=True)
class FullChargeScore:
    """The day's forecast full charge (minutes of the day)."""

    planned_at: datetime
    start_soc: float | None
    system: int | None  # None = forecast not full before T2
    optimal: int | None
    actual: int | None
    hours: tuple[int, ...]  # discharge hours the plan qualified
    optimal_hours: tuple[int, ...]  # the same with the optimal full charge
    # The plan's hours recomputed here — equal to `hours` unless the slot
    # window was not the default 07-10 (then `optimal_hours` is approximate).
    hours_recomputed: tuple[int, ...]
    s0_soc: float | None = None  # what the battery really had at S0
    actual_start: int | None = None  # the charge start that struck (or S0)
    actual_start_soc: float | None = None
    model: int | None = None  # the charging model from the actual start and SoC

    @property
    def error_min(self) -> int | None:
        """System − optimal; + = forecast full later than what came allowed."""
        if self.system is None or self.optimal is None:
            return None
        return self.system - self.optimal

    @property
    def error_actual_min(self) -> int | None:
        if self.system is None or self.actual is None:
            return None
        return self.system - self.actual

    @property
    def model_error_min(self) -> int | None:
        """Model − actual; + = the charging model full later than reality."""
        if self.model is None or self.actual is None:
            return None
        return self.model - self.actual

    @property
    def hours_changed(self) -> bool:
        return self.hours_recomputed != self.optimal_hours

    def to_dict(self) -> dict[str, Any]:
        return {
            "planned_at": self.planned_at.isoformat(),
            "start_soc": self.start_soc,
            "system": self.system,
            "optimal": self.optimal,
            "actual": self.actual,
            "hours": list(self.hours),
            "optimal_hours": list(self.optimal_hours),
            "hours_recomputed": list(self.hours_recomputed),
            "s0_soc": self.s0_soc,
            "actual_start": self.actual_start,
            "actual_start_soc": self.actual_start_soc,
            "model": self.model,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FullChargeScore:
        return cls(
            planned_at=datetime.fromisoformat(data["planned_at"]),
            start_soc=data.get("start_soc"),
            system=data.get("system"),
            optimal=data.get("optimal"),
            actual=data.get("actual"),
            hours=tuple(data.get("hours", [])),
            optimal_hours=tuple(data.get("optimal_hours", [])),
            hours_recomputed=tuple(data.get("hours_recomputed", [])),
            s0_soc=data.get("s0_soc"),
            actual_start=data.get("actual_start"),
            actual_start_soc=data.get("actual_start_soc"),
            model=data.get("model"),
        )
