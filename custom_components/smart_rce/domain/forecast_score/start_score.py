"""D2 — the charge start: when the battery starts taking PV, scored per moment.

The rule is production's `find_candidate`: the latest start up to S0 whose
forecast surplus in its price window still fills the battery from the SoC of
the moment. The benchmark is the same rule on what really came.

Moments: 07:41, 08:41, 09:16, 09:41 as a preview — a "too late" early in the
morning is reversible, the rule re-runs every 5 minutes — and, the one that
counts, `start`: the last evaluation before the start actually struck ("too
early" there is not reversible; the start locks).

Like the reserve, the production error splits exactly (in hours):
- PV part = start(forecast PV, model) − start(actual PV, model) — the ranking;
- consumption part = start(actual PV, model) − start(actual PV, actual cons);
`model` = production's consumption model (p60 of earlier days of the same type).
Next to every PV call the same with ACTUAL consumption on both sides
(diagnostic). The kWh of a call (shortfall when too late, stored too early)
always come from what really happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from ..charge_curve import kwh_to_full
from ..charge_start_shift import (
    FIRST_EVALUATION,
    Candidate,
    find_candidate,
    price_window_end,
    to_minutes,
)
from ..pv_surplus import SLOT_MINUTES, SurplusProfile, minute_of_day
from .consumption_models import ConsumptionModel
from .day_actuals import CONSUMPTION, PV
from .production_setup import CURRENT, ProductionSetup
from .variant_results import SCORED_VARIANTS, consumed

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ..pv_forecast import PvForecastResult
    from .day_actuals import DayActuals
    from .variant_results import ScoredSnapshot

PREVIEW_MOMENTS: Final = (
    ("07:41", time(7, 41)),
    ("08:41", time(8, 41)),
    ("09:16", time(9, 16)),
    ("09:41", time(9, 41)),
)
START_MOMENT: Final = "start"
# Production evaluates at :01, :06, :11 ... (every 5 minutes, minute % 5 == 1).
_EVALUATION_STEP: Final = 5
_EVALUATION_OFFSET: Final = 1
# A start within this of the benchmark counts as on time.
ON_TIME_HOURS: Final = 0.5


def score_start(  # noqa: PLR0917 — one call site, inputs of one day
    actuals: DayActuals,
    snapshots: Sequence[ScoredSnapshot],
    prices: Sequence[float],
    s0: int,
    earlier_same_type: Sequence[Mapping[int, float]],
    actual_start: int | None,
    setup: ProductionSetup = CURRENT,
) -> tuple[MomentStart, ...]:
    """Every charge-start moment of the day that came before S0.

    Variants are ranked with today's setup (`CURRENT`); `setup` is the one
    production ran on that day — its call is the facts panel's.
    """
    pv = actuals.half_hours(PV)
    cons = actuals.half_hours(CONSUMPTION)
    world = _World(
        day=actuals.day,
        prices=tuple(prices),
        s0=s0,
        pv=pv,
        actual=SurplusProfile.build(actuals.day, pv, cons),
        cons=cons,
        model=CURRENT.start_model.half_hours(earlier_same_type),
        earlier=earlier_same_type,
        setup=setup,
        setup_model=setup.start_model.half_hours(earlier_same_type),
    )
    moments = [(label, to_minutes(at)) for label, at in PREVIEW_MOMENTS]
    decided = last_evaluation_before(actual_start) if actual_start is not None else None
    if decided is not None:
        moments.append((START_MOMENT, decided))
    out: list[MomentStart] = []
    for label, minute in moments:
        if minute >= s0:
            continue  # production evaluates only before S0
        at = actuals.at(minute)
        snapshot = consumed(snapshots, at)
        soc = actuals.soc_near(at)
        if snapshot is None or soc is None:
            continue
        out.append(world.score(label, minute, snapshot, soc))
    return tuple(out)


def last_evaluation_before(start: int) -> int | None:
    """Return the last 5-minute evaluation before `start` (None before 07:41)."""
    minute = start - 1
    minute -= (minute - _EVALUATION_OFFSET) % _EVALUATION_STEP
    return minute if minute >= FIRST_EVALUATION else None


def forecast_half_hours(result: PvForecastResult, day: date) -> dict[int, float]:
    """Forecast energy per half hour of `day` — the archive holds RATES (kWh/h)."""
    out: dict[int, float] = {}
    for period in result.forecast:
        moment = datetime.fromisoformat(str(period.period_start).replace(" ", "T"))
        if moment.date() == day:
            slot = int(minute_of_day(moment)) // SLOT_MINUTES
            out[slot] = out.get(slot, 0.0) + period.pv_estimate_adjusted / 2
    return out


@dataclass(frozen=True)
class _World:
    """One day's fixed inputs: prices, S0, what came, the consumption model."""

    day: date
    prices: tuple[float, ...]
    s0: int
    pv: dict[int, float]
    actual: SurplusProfile
    cons: dict[int, float]
    model: dict[int, float] | None  # None = flat 0.9 kWh/h (no history yet)
    earlier: Sequence[Mapping[int, float]]
    setup: ProductionSetup
    setup_model: dict[int, float] | None

    def score(
        self, label: str, now: int, snapshot: ScoredSnapshot, soc: float
    ) -> MomentStart:
        need = kwh_to_full(soc)
        star_actual = self._candidate(self.pv, self.cons, now, need)
        star_model = self._candidate(self.pv, self.model, now, need)
        variants: list[VariantStart] = []
        for variant in SCORED_VARIANTS:
            result = snapshot.result(variant)
            if result is None:
                continue
            fc = forecast_half_hours(result, self.day)
            if not fc:
                continue
            variants.append(
                VariantStart(
                    variant=variant,
                    call=self._call(
                        self._candidate(fc, self.model, now, need), star_model, need
                    ),
                    call_actual_cons=self._call(
                        self._candidate(fc, self.cons, now, need), star_actual, need
                    ),
                )
            )
        production_fc = self._forecast(snapshot, CURRENT.pv_variant)
        setup_fc = self._forecast(snapshot, self.setup.pv_variant)
        production = (
            ProductionStart(
                setup=self.setup.label,
                call=self._call(
                    self._candidate(setup_fc, self.setup_model, now, need),
                    star_actual,
                    need,
                ),
                benchmark_model=self._candidate(
                    self.pv, self.setup_model, now, need
                ).start,
            )
            if setup_fc
            else None
        )
        return MomentStart(
            label=label,
            minute=now,
            snapshot_at=snapshot.taken_at,
            soc=soc,
            need_kwh=round(need, 3),
            benchmark=star_actual.start,
            benchmark_enough=star_actual.enough,
            benchmark_model=star_model.start,
            production=production,
            variants=tuple(variants),
            models=self._models(now, need, star_actual, production_fc),
        )

    def _forecast(self, snapshot: ScoredSnapshot, variant: str) -> dict[int, float]:
        result = snapshot.result(variant)
        return forecast_half_hours(result, self.day) if result is not None else {}

    def _models(
        self,
        now: int,
        need: float,
        star_actual: Candidate,
        production_fc: dict[int, float],
    ) -> tuple[ModelStart, ...]:
        """Consumption models with ACTUAL PV (ranking) and production PV."""
        out: list[ModelStart] = []
        for model in ConsumptionModel:
            if not model.is_profile:
                continue
            profile = model.half_hours(self.earlier)
            if profile is None and model is not ConsumptionModel.FLAT:
                continue
            out.append(
                ModelStart(
                    model=model.key,
                    call=self._call(
                        self._candidate(self.pv, profile, now, need), star_actual, need
                    ),
                    call_production_pv=(
                        self._call(
                            self._candidate(production_fc, profile, now, need),
                            star_actual,
                            need,
                        )
                        if production_fc
                        else None
                    ),
                )
            )
        return tuple(out)

    def _candidate(
        self,
        pv: Mapping[int, float],
        cons: Mapping[int, float] | None,
        now: int,
        need: float,
    ) -> Candidate:
        profile = SurplusProfile.build(self.day, pv, cons)
        return find_candidate(profile, self.prices, now, need, self.s0)

    def _call(self, cand: Candidate, star: Candidate, need: float) -> StartCall:
        """Classify a candidate against a benchmark; kWh from what really came."""
        delta_h = (cand.start - star.start) / 60
        if not cand.enough and not star.enough:
            verdict = StartVerdict.RIGHTLY_SHORT
        elif not cand.enough:
            verdict = StartVerdict.WRONGLY_SHORT
        elif abs(delta_h) < ON_TIME_HOURS:
            verdict = StartVerdict.ON_TIME
        elif delta_h > 0:
            verdict = StartVerdict.TOO_LATE
        else:
            verdict = StartVerdict.TOO_EARLY
        end = price_window_end(cand.start, self.prices) * 60
        shortfall = (
            max(0.0, need - self.actual.total(cand.start, end))
            if verdict is StartVerdict.TOO_LATE
            else 0.0
        )
        early = self.actual.total(cand.start, star.start) if delta_h < 0 else 0.0
        return StartCall(
            start=cand.start,
            enough=cand.enough,
            verdict=verdict,
            delta_h=round(delta_h, 3),
            shortfall_kwh=round(shortfall, 3),
            early_kwh=round(early, 3),
        )


# --- Value objects --- #


class StartVerdict(StrEnum):
    """How a start compares with the benchmark."""

    ON_TIME = "on_time"
    TOO_EARLY = "too_early"
    TOO_LATE = "too_late"
    RIGHTLY_SHORT = "rightly_short"  # "not enough" — and indeed not enough
    WRONGLY_SHORT = "wrongly_short"  # "not enough" — but the sun did fill it


@dataclass(frozen=True)
class StartCall:
    """One start against its benchmark (minutes of the day, hours, kWh)."""

    start: int
    enough: bool
    verdict: StartVerdict
    delta_h: float  # + = later than the benchmark
    shortfall_kwh: float  # too late: what the battery missed
    early_kwh: float  # too early: surplus stored before the benchmark start

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "enough": self.enough,
            "verdict": str(self.verdict),
            "delta_h": self.delta_h,
            "shortfall_kwh": self.shortfall_kwh,
            "early_kwh": self.early_kwh,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StartCall:
        return cls(
            start=int(data["start"]),
            enough=bool(data["enough"]),
            verdict=StartVerdict(data["verdict"]),
            delta_h=float(data["delta_h"]),
            shortfall_kwh=float(data["shortfall_kwh"]),
            early_kwh=float(data["early_kwh"]),
        )


@dataclass(frozen=True)
class VariantStart:
    """One PV forecast at one moment: production model (ranking), actual cons."""

    variant: str
    call: StartCall
    call_actual_cons: StartCall

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "call": self.call.to_dict(),
            "call_actual_cons": self.call_actual_cons.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VariantStart:
        return cls(
            variant=str(data["variant"]),
            call=StartCall.from_dict(data["call"]),
            call_actual_cons=StartCall.from_dict(data["call_actual_cons"]),
        )


@dataclass(frozen=True)
class ModelStart:
    """One consumption model at one moment: actual PV (ranking), production PV."""

    model: str
    call: StartCall
    call_production_pv: StartCall | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "call": self.call.to_dict(),
            "call_production_pv": (
                self.call_production_pv.to_dict()
                if self.call_production_pv is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ModelStart:
        raw = data.get("call_production_pv")
        return cls(
            model=str(data["model"]),
            call=StartCall.from_dict(data["call"]),
            call_production_pv=StartCall.from_dict(raw) if raw is not None else None,
        )


@dataclass(frozen=True)
class ProductionStart:
    """Production's call with the setup of the day, and its benchmark with that model.

    Δ = PV part (call − benchmark_model) + consumption part (benchmark_model −
    benchmark of actual PV and consumption), in hours.
    """

    setup: str
    call: StartCall  # against the benchmark of actual PV + actual consumption
    benchmark_model: int  # the rule on actual PV + the day's consumption model

    @property
    def pv_part_h(self) -> float:
        return round((self.call.start - self.benchmark_model) / 60, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup": self.setup,
            "call": self.call.to_dict(),
            "benchmark_model": self.benchmark_model,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProductionStart:
        return cls(
            setup=str(data["setup"]),
            call=StartCall.from_dict(data["call"]),
            benchmark_model=int(data["benchmark_model"]),
        )


@dataclass(frozen=True)
class MomentStart:
    """The charge start at one moment: benchmarks, production, per variant and model."""

    label: str
    minute: int
    snapshot_at: datetime
    soc: float
    need_kwh: float
    benchmark: int  # the rule on actual PV + actual consumption
    benchmark_enough: bool
    benchmark_model: int  # the rule on actual PV + today's consumption model
    production: ProductionStart | None  # the day's setup vs `benchmark`
    variants: tuple[VariantStart, ...]
    models: tuple[ModelStart, ...]

    @property
    def pv_part_h(self) -> float | None:
        if self.production is None:
            return None
        return self.production.pv_part_h

    @property
    def consumption_part_h(self) -> float | None:
        if self.production is None:
            return None
        return round((self.production.benchmark_model - self.benchmark) / 60, 3)

    def variant(self, key: str) -> VariantStart | None:
        return next((v for v in self.variants if v.variant == key), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "minute": self.minute,
            "snapshot_at": self.snapshot_at.isoformat(),
            "soc": self.soc,
            "need_kwh": self.need_kwh,
            "benchmark": self.benchmark,
            "benchmark_enough": self.benchmark_enough,
            "benchmark_model": self.benchmark_model,
            "production": (
                self.production.to_dict() if self.production is not None else None
            ),
            "variants": [v.to_dict() for v in self.variants],
            "models": [m.to_dict() for m in self.models],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MomentStart:
        raw = data.get("production")
        return cls(
            label=str(data["label"]),
            minute=int(data["minute"]),
            snapshot_at=datetime.fromisoformat(data["snapshot_at"]),
            soc=float(data["soc"]),
            need_kwh=float(data["need_kwh"]),
            benchmark=int(data["benchmark"]),
            benchmark_enough=bool(data["benchmark_enough"]),
            benchmark_model=int(data["benchmark_model"]),
            production=(
                ProductionStart.from_dict(raw)
                if isinstance(raw, dict) and "call" in raw
                else None
            ),
            variants=tuple(VariantStart.from_dict(v) for v in data["variants"]),
            models=tuple(ModelStart.from_dict(m) for m in data["models"]),
        )
