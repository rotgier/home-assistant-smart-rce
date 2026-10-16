"""DayScore — one day of the forecast scoreboard: decisions scored, facts kept.

A day is scored twice and checked once:
- PRELIMINARY at 16:06 — everything up to 13:00 is final already; a charge-start
  window reaching past 16:00 is not;
- FINAL at 21:06 — after sunset nothing that matters moves (the D2 windows and
  the afternoon top-up close before the evening T2);
- the 00:20 CHECK rescores from the energy archive (written 00:15); a
  different result (say, a gap in the statistics) replaces the final one.
Rankings read final scores only.

Pure: every input is passed in — the archive, the recorder and the workday
calendar are the adapters' business.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Final

from ..battery_schedule.entry import WORKDAYS_ONLY_EXTRA_DAYS
from ..pv_surplus import DayType
from .day_type import DayTypeFigures, day_type_figures
from .full_charge_score import FullChargeScore, PlanReading, score_full_charge
from .production_outcome import ChargeStartFact, ProductionOutcome, production_outcome
from .production_setup import setup_on
from .reserve_score import (
    DISCHARGE_LINE_MOMENT,
    MAIN_MOMENT,
    ModelReserve,
    MomentReserve,
    score_reserve,
)
from .start_score import START_MOMENT, MomentStart, score_start
from .variant_results import consumed

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .day_actuals import DayActuals
    from .variant_results import ScoredSnapshot

# 2: production scored with the setup of its day (`production_setup`).
# 3: reserve errors signed system − needed (− = too little), as the charge start.
# 4: the variant ranking with the hour credit (0f65863) on every day.
# Older records are ignored, so the startup catch-up rescores those days.
SCHEMA_VERSION: Final = 4


class ScoreStatus(StrEnum):
    PRELIMINARY = "preliminary"
    FINAL = "final"


def has_morning_reserve(day: date) -> bool:
    """Workdays, and the days the morning auto slots ran by exception."""
    return DayType.of(day) is DayType.WORKDAY or day in WORKDAYS_ONLY_EXTRA_DAYS


@dataclass(frozen=True)
class DayInputs:
    """Everything a day is scored from (the adapters gather it)."""

    actuals: DayActuals
    snapshots: Sequence[ScoredSnapshot]
    # Consumption without water heating per half hour (key 0..47) of the days
    # production learned from: the T1 reserve's earlier workdays, and the
    # charge start's earlier days of the same type.
    earlier_workdays: Mapping[date, Mapping[int, float]]
    earlier_same_type: Mapping[date, Mapping[int, float]]
    prices: Sequence[float] | None  # RCE per hour, 24 values
    s0: int | None  # cheapest-hours charge start, minute of the day
    start: ChargeStartFact
    # CHARGE_AFTERNOON spans (minutes of the day); None = history unavailable.
    afternoon_charge: Sequence[tuple[int, int]] | None
    # The morning discharge plan just before its first hour started (D3).
    morning_plan: PlanReading | None = None


def score_day(
    inputs: DayInputs,
    *,
    status: ScoreStatus,
    computed_at: datetime,
    code_version: str,
) -> DayScore:
    """Score both decisions of the day and gather its facts."""
    actuals = inputs.actuals
    day = actuals.day
    setup = setup_on(day, actuals.tz)
    reserve: tuple[MomentReserve, ...] = ()
    models: tuple[ModelReserve, ...] = ()
    if has_morning_reserve(day):
        reserve, models = score_reserve(
            actuals, inputs.snapshots, list(inputs.earlier_workdays.values()), setup
        )
    start: tuple[MomentStart, ...] = ()
    if inputs.prices is not None and inputs.s0 is not None and len(inputs.prices) == 24:
        start = score_start(
            actuals,
            inputs.snapshots,
            inputs.prices,
            inputs.s0,
            list(inputs.earlier_same_type.values()),
            inputs.start.start,
            setup,
        )
    main = consumed(inputs.snapshots, datetime.combine(day, MAIN_MOMENT.at, actuals.tz))
    outcome = production_outcome(actuals, inputs.start, inputs.afternoon_charge)
    full_charge = (
        score_full_charge(
            actuals,
            inputs.morning_plan,
            inputs.prices,
            inputs.s0,
            outcome.full_at,
            inputs.start.start,
        )
        if inputs.morning_plan is not None
        and inputs.prices is not None
        and inputs.s0 is not None
        and len(inputs.prices) == 24
        else None
    )
    return DayScore(
        day=day,
        status=status,
        computed_at=computed_at,
        code_version=code_version,
        day_type=day_type_figures(actuals, main),
        reserve=reserve,
        reserve_models=models,
        start=start,
        outcome=outcome,
        earlier_workdays=tuple(sorted(inputs.earlier_workdays)),
        earlier_same_type=tuple(sorted(inputs.earlier_same_type)),
        snapshot_sources=tuple(sorted({s.source for s in inputs.snapshots})),
        s0=inputs.s0,
        setup=setup.label,
        full_charge=full_charge,
    )


@dataclass(frozen=True)
class DayScore:
    """The scored day (value object, persisted one record per scoring run)."""

    KIND: ClassVar[str] = "forecast_day_score"

    day: date
    status: ScoreStatus
    computed_at: datetime
    code_version: str
    day_type: DayTypeFigures
    reserve: tuple[MomentReserve, ...]
    reserve_models: tuple[ModelReserve, ...]
    start: tuple[MomentStart, ...]
    outcome: ProductionOutcome
    earlier_workdays: tuple[date, ...]
    earlier_same_type: tuple[date, ...]
    snapshot_sources: tuple[str, ...]
    s0: int | None
    # Filled by the 00:20 check when it replaced the 21:06 result: what moved.
    replaced: tuple[str, ...] = ()
    # The production setup the day ran on (`production_setup`).
    setup: str = ""
    # D3: the forecast full charge of the morning discharge (from 17.10.2026).
    full_charge: FullChargeScore | None = None

    # --- headline figures (the long-term statistics sensors) --- #

    def reserve_moment(self, label: str) -> MomentReserve | None:
        return next((m for m in self.reserve if m.label == label), None)

    def start_moment(self, label: str) -> MomentStart | None:
        return next((m for m in self.start if m.label == label), None)

    @property
    def reserve_error_pp(self) -> int | None:
        """System's T1 reserve error at 06:11 (asked − needed); − = too little."""
        main = self.reserve_moment(MAIN_MOMENT.label)
        return main.production_error_pp if main is not None else None

    @property
    def reserve_error_pv_pp(self) -> int | None:
        main = self.reserve_moment(MAIN_MOMENT.label)
        return main.pv_part_pp if main is not None else None

    @property
    def reserve_error_consumption_pp(self) -> int | None:
        main = self.reserve_moment(MAIN_MOMENT.label)
        return main.consumption_part_pp if main is not None else None

    @property
    def reserve_error_margin_pp(self) -> int | None:
        main = self.reserve_moment(MAIN_MOMENT.label)
        return main.margin_part_pp if main is not None else None

    @property
    def full_charge_error_min(self) -> int | None:
        """Forecast full charge − optimal, minutes (+ = forecast later)."""
        return self.full_charge.error_min if self.full_charge is not None else None

    @property
    def reserve_line_error_pp(self) -> int | None:
        """The rest-of-T1 reserve at 08:41 — the morning discharge line."""
        line = self.reserve_moment(DISCHARGE_LINE_MOMENT)
        return line.production_error_pp if line is not None else None

    @property
    def start_delta_h(self) -> float | None:
        """Production's start call at the moment it struck vs the benchmark (h)."""
        moment = self.start_moment(START_MOMENT)
        if moment is None or moment.production is None:
            return None
        return moment.production.call.delta_h

    # --- persistence --- #

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": self.KIND,
            "day": self.day.isoformat(),
            "status": str(self.status),
            "computed_at": self.computed_at.isoformat(),
            "code_version": self.code_version,
            "day_type": self.day_type.to_dict(),
            "reserve": [m.to_dict() for m in self.reserve],
            "reserve_models": [m.to_dict() for m in self.reserve_models],
            "start": [m.to_dict() for m in self.start],
            "outcome": self.outcome.to_dict(),
            "earlier_workdays": [d.isoformat() for d in self.earlier_workdays],
            "earlier_same_type": [d.isoformat() for d in self.earlier_same_type],
            "snapshot_sources": list(self.snapshot_sources),
            "s0": self.s0,
            "replaced": list(self.replaced),
            "setup": self.setup,
            "full_charge": (
                self.full_charge.to_dict() if self.full_charge is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DayScore:
        return cls(
            day=date.fromisoformat(data["day"]),
            status=ScoreStatus(data["status"]),
            computed_at=datetime.fromisoformat(data["computed_at"]),
            code_version=str(data.get("code_version", "unknown")),
            day_type=DayTypeFigures.from_dict(data.get("day_type", {})),
            reserve=tuple(MomentReserve.from_dict(m) for m in data["reserve"]),
            reserve_models=tuple(
                ModelReserve.from_dict(m) for m in data["reserve_models"]
            ),
            start=tuple(MomentStart.from_dict(m) for m in data["start"]),
            outcome=ProductionOutcome.from_dict(data["outcome"]),
            earlier_workdays=tuple(
                date.fromisoformat(d) for d in data.get("earlier_workdays", [])
            ),
            earlier_same_type=tuple(
                date.fromisoformat(d) for d in data.get("earlier_same_type", [])
            ),
            snapshot_sources=tuple(data.get("snapshot_sources", [])),
            s0=data.get("s0"),
            replaced=tuple(data.get("replaced", [])),
            setup=str(data.get("setup", "")),
            full_charge=(
                FullChargeScore.from_dict(raw)
                if isinstance(raw := data.get("full_charge"), dict)
                else None
            ),
        )

    @classmethod
    def latest_of(cls, records: Sequence[Mapping[str, Any]]) -> DayScore | None:
        """Pick the day's score from its records: the last final, else the last one."""
        scores = [
            cls.from_dict(r)
            for r in records
            if r.get("kind") == cls.KIND
            and r.get("schema_version", 1) == SCHEMA_VERSION
        ]
        finals = [s for s in scores if s.status is ScoreStatus.FINAL]
        if finals:
            return finals[-1]
        return scores[-1] if scores else None

    def differences(self, other: DayScore) -> tuple[str, ...]:
        """Top-level parts that differ from `other` (status and timestamps aside)."""
        mine, theirs = self.to_dict(), other.to_dict()
        ignored = {"status", "computed_at", "code_version", "replaced"}
        return tuple(
            key
            for key in mine
            if key not in ignored and mine.get(key) != theirs.get(key)
        )
