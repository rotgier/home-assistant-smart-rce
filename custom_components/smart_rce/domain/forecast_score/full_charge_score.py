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
The surplus error splits exactly, through `with_model` — actual PV with the
consumption model production read (p60 of earlier days of the same type):
PV part = system − with_model, consumption part = with_model − optimal.
`hours` vs `optimal_hours`: the discharge hours each full charge qualifies —
does the forecast error change what was sold?

`moments`: the same surplus error at the moments the other decisions are
scored at (06:11, 06:41, the charge start's 07:41 … and the last evaluation
before the actual start), up to the charge start. The plan is rebuilt every
minute until then, so `system` is its `full_at` in force at that minute, 1:1
from production; optimal and the split as above with that moment's start SoC.
S0 is the day's (`s0`): a shift that moved S0 during the morning is not
followed. The charging model has no moments — it reads no forecast.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any, Final

from ..battery_schedule.morning_discharge import MorningDischargePlan
from ..charge_start_shift import full_charge_minute
from ..pv_surplus import SurplusProfile, surplus_window_end
from ..rce import RceDayPrices
from .day_actuals import CONSUMPTION, PV
from .reserve_score import MAIN_MOMENT
from .start_score import PREVIEW_MOMENTS, START_MOMENT, last_evaluation_before

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .day_actuals import DayActuals

# The slot's default window (07:00-10:00): the dearest hour is picked in it.
_WINDOW_FIRST_HOUR = 7
_WINDOW_END_HOUR = 10
# The reserve's moments from 06:11, then the charge start's.
MOMENTS: Final = (
    (MAIN_MOMENT.label, MAIN_MOMENT.at),
    ("06:41", time(6, 41)),
    *PREVIEW_MOMENTS,
)


@dataclass(frozen=True)
class PlanReading:
    """The morning discharge plan as it stood at one moment (from its sensor)."""

    at: datetime
    full_at: int | None  # minute of the day; None = not full before T2
    hours: tuple[int, ...]
    # What the plan charged from: the reserve without margin, the 10% floor
    # on a day off (b6343a5).
    start_soc: float | None
    day_off: bool = False


def decided_plan(readings: Sequence[PlanReading], day: date) -> PlanReading | None:
    """Return the plan in force when its first discharge hour began (it decided)."""
    decided: PlanReading | None = None
    for reading in readings:
        if not reading.hours or reading.at.date() != day:
            continue
        if reading.at.hour * 60 + reading.at.minute < reading.hours[0] * 60:
            decided = reading
    return decided


def score_full_charge(  # noqa: PLR0917 — one call site, the day's facts
    actuals: DayActuals,
    plan: PlanReading,
    prices: Sequence[float],
    s0: int,
    actual_full: int | None,
    actual_start: int | None,
    consumption_model: Mapping[int, float] | None = None,
    readings: Sequence[PlanReading] = (),
) -> FullChargeScore:
    """Score the plan's full charge against the same computation on what came.

    `readings`: the plan as it stood over the morning (oldest first), for the
    moments.
    """
    day = actuals.day
    t2 = surplus_window_end(day) * 60
    day_prices = RceDayPrices(None, day, tuple(prices))
    came = SurplusProfile.build(
        day, actuals.half_hours(PV), actuals.half_hours(CONSUMPTION)
    )
    # Actual PV, the model's consumption (None: the flat production falls back to).
    modelled = SurplusProfile.build(day, actuals.half_hours(PV), consumption_model)

    def from_s0(profile: SurplusProfile, soc: float | None) -> int | None:
        return full_charge_minute(profile, s0, soc, t2) if soc is not None else None

    optimal = from_s0(came, plan.start_soc)
    with_model = from_s0(modelled, plan.start_soc)
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
        with_model=with_model,
        actual_start=start,
        actual_start_soc=start_soc,
        model=model,
        moments=tuple(
            FullChargeMoment(
                label=label,
                at=minute,
                start_soc=reading.start_soc,
                system=reading.full_at,
                optimal=from_s0(came, reading.start_soc),
                with_model=from_s0(modelled, reading.start_soc),
            )
            for label, minute, reading in _readings_at_moments(
                readings, day, actual_start if actual_start is not None else s0
            )
        ),
    )


def _readings_at_moments(
    readings: Sequence[PlanReading], day: date, charge_start: int
) -> list[tuple[str, int, PlanReading]]:
    """Pair each moment before the charge start with the plan in force then."""
    moments = [(label, at.hour * 60 + at.minute) for label, at in MOMENTS]
    last = last_evaluation_before(charge_start)
    if last is not None:
        moments.append((START_MOMENT, last))
    out: list[tuple[str, int, PlanReading]] = []
    for label, minute in moments:
        if minute >= charge_start:
            continue
        in_force = [
            r
            for r in readings
            if r.at.date() == day and r.at.hour * 60 + r.at.minute <= minute
        ]
        if in_force:
            out.append((label, minute, in_force[-1]))
    return out


def _hours(
    prices: RceDayPrices, best: int, s0: int, t2: int, full: int | None
) -> tuple[int, ...]:
    """Discharge hours a full charge at `full` qualifies (as production rounds it)."""
    refill_end = t2 // 60 if full is None else min(t2 // 60, -(-full // 60))
    return MorningDischargePlan.hours_from(prices, best, s0 // 60, refill_end)


@dataclass(frozen=True)
class FullChargeMoment:
    """The surplus forecast's full charge as the plan stood at one moment."""

    label: str
    at: int  # minute of the day
    start_soc: float | None
    system: int | None  # the plan's full_at then; None = not full before T2
    optimal: int | None
    with_model: int | None

    @property
    def error_min(self) -> int | None:
        """System − optimal; + = forecast full later than what came allowed."""
        if self.system is None or self.optimal is None:
            return None
        return self.system - self.optimal

    @property
    def pv_part_min(self) -> int | None:
        """The PV forecast's share of `error_min`."""
        if self.system is None or self.with_model is None:
            return None
        return self.system - self.with_model

    @property
    def consumption_part_min(self) -> int | None:
        """The consumption model's share of `error_min`."""
        if self.with_model is None or self.optimal is None:
            return None
        return self.with_model - self.optimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "at": self.at,
            "start_soc": self.start_soc,
            "system": self.system,
            "optimal": self.optimal,
            "with_model": self.with_model,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FullChargeMoment:
        return cls(
            label=str(data["label"]),
            at=int(data["at"]),
            start_soc=data.get("start_soc"),
            system=data.get("system"),
            optimal=data.get("optimal"),
            with_model=data.get("with_model"),
        )


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
    with_model: int | None = None  # actual PV + the consumption model (the split)
    # The surplus error at the moments before the charge start.
    moments: tuple[FullChargeMoment, ...] = ()

    @property
    def error_min(self) -> int | None:
        """System − optimal; + = forecast full later than what came allowed."""
        if self.system is None or self.optimal is None:
            return None
        return self.system - self.optimal

    @property
    def pv_part_min(self) -> int | None:
        """The PV forecast's share of `error_min` (system − actual PV with the model)."""
        if self.system is None or self.with_model is None:
            return None
        return self.system - self.with_model

    @property
    def consumption_part_min(self) -> int | None:
        """The consumption model's share of `error_min` (with_model − optimal)."""
        if self.with_model is None or self.optimal is None:
            return None
        return self.with_model - self.optimal

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

    def plan_readings(self, *, day_off: bool) -> list[PlanReading]:
        """Rebuild the plan readings this score was computed from, oldest first.

        For a rescore once the recorder has dropped the plan's history: the
        decision with its hours, and each moment as the plan stood then
        (without hours — a moment never stands in for the decision).
        """
        day = self.planned_at.date()
        tz = self.planned_at.tzinfo
        readings = [
            PlanReading(
                at=datetime.combine(day, time(m.at // 60, m.at % 60), tz),
                full_at=m.system,
                hours=(),
                start_soc=m.start_soc,
                day_off=day_off,
            )
            for m in self.moments
        ]
        readings.append(
            PlanReading(
                at=self.planned_at,
                full_at=self.system,
                hours=self.hours,
                start_soc=self.start_soc,
                day_off=day_off,
            )
        )
        return sorted(readings, key=lambda r: r.at)

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
            "with_model": self.with_model,
            "moments": [m.to_dict() for m in self.moments],
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
            with_model=data.get("with_model"),
            moments=tuple(
                FullChargeMoment.from_dict(m) for m in data.get("moments", [])
            ),
        )
