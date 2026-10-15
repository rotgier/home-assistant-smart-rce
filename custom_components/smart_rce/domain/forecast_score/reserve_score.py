"""D1 — the T1 reserve: how much SoC to keep for 07:00-13:00, scored per moment.

Moments: 06:11 (the main one — first Solcast fetch of the day, the morning
charge has its target), 06:41 (last chance to charge less), and 07:41 / 08:41
/ 09:41 — the reserve for the REST of the window, the line a morning discharge
must not cross. Before 07:00 the whole window counts; inside it production
reads now-aware views (closed buckets 0, the in-progress one from live power),
and so does the score (`target_soc_for`, the path `TargetSoc` takes).

Each moment reads the snapshot the decision consumed. Signs: + = the forecast
asked for LESS reserve than was needed (too optimistic — the battery would be
short), in SoC points.

The production error splits exactly into two parts:
- PV part  = R(actual PV, model) − R(forecast PV, model) — the variant ranking;
- consumption part = R(actual PV, actual cons) − R(actual PV, model);
where `model` is production's consumption model (p60 of earlier workdays).
Next to every PV part: the same with ACTUAL consumption on both sides, and the
difference — how much the consumption model colours the PV score.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any, Final

from ..battery_schedule.t1_reserve import T1SocReserve
from ..bucket import PV_WINDOW_HOURS
from ..consumption_profiles import ConsumptionProfile
from ..target_soc import (
    BATTERY_CAPACITY_KWH,
    LOSS_FACTOR,
    MIN_SOC_PERCENT,
    PvProfile,
    TargetSocContext,
    TargetSocResult,
    _calculate_target_soc,
    count_from,
    now_aware_consumption,
    target_soc_for,
)
from .consumption_models import PRODUCTION_MODEL, ConsumptionModel, window_profile
from .day_actuals import CONSUMPTION, PV
from .variant_results import PRODUCTION, SCORED_VARIANTS, consumed

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .day_actuals import DayActuals
    from .variant_results import ScoredSnapshot


@dataclass(frozen=True)
class ReserveMoment:
    """A decision moment of the T1 reserve."""

    label: str
    at: time


MAIN_MOMENT: Final = ReserveMoment("06:11", time(6, 11))
RESERVE_MOMENTS: Final = (
    MAIN_MOMENT,
    ReserveMoment("06:41", time(6, 41)),
    ReserveMoment("07:41", time(7, 41)),
    ReserveMoment("08:41", time(8, 41)),  # the discharge line
    ReserveMoment("09:41", time(9, 41)),
)
DISCHARGE_LINE_MOMENT: Final = "08:41"


def score_reserve(
    actuals: DayActuals,
    snapshots: Sequence[ScoredSnapshot],
    earlier_workdays: Sequence[Mapping[int, float]],
) -> tuple[tuple[MomentReserve, ...], tuple[ModelReserve, ...]]:
    """Every reserve moment of the day, and the consumption models at 06:11."""
    model = PRODUCTION_MODEL.reserve_profile(earlier_workdays)
    if model is None:  # production falls back to flat without history
        model = ConsumptionProfile.flat()
    moments: list[MomentReserve] = []
    for moment in RESERVE_MOMENTS:
        snapshot = consumed(
            snapshots, datetime.combine(actuals.day, moment.at, actuals.tz)
        )
        if snapshot is not None:
            scored = _score_moment(moment, snapshot, actuals, model)
            if scored is not None:
                moments.append(scored)
    main = next((m for m in moments if m.label == MAIN_MOMENT.label), None)
    models = (
        _score_models(actuals, snapshots, earlier_workdays, main)
        if main is not None
        else ()
    )
    return tuple(moments), models


def _score_moment(
    moment: ReserveMoment,
    snapshot: ScoredSnapshot,
    actuals: DayActuals,
    model: ConsumptionProfile,
) -> MomentReserve | None:
    ctx = _target_ctx(actuals.day, snapshot)
    views = _ActualViews(actuals, ctx)
    needed = views.target(views.model(views.cons))
    with_model = views.target(views.model(model))
    production_result = snapshot.result(PRODUCTION)
    production = (
        target_soc_for(production_result, model, ctx, is_today=True)
        if production_result is not None
        else None
    )
    variants: list[VariantReserve] = []
    for variant in SCORED_VARIANTS:
        result = snapshot.result(variant)
        if result is None:
            continue
        on_model = target_soc_for(result, model, ctx, is_today=True)
        on_actual = target_soc_for(result, views.cons, ctx, is_today=True)
        if on_model is None or on_actual is None:
            continue
        variants.append(
            VariantReserve(
                variant=variant,
                reserve=on_model.value,
                pv_error_pp=with_model.value - on_model.value,
                pv_error_actual_cons_pp=needed.value - on_actual.value,
            )
        )
    return MomentReserve(
        label=moment.label,
        snapshot_at=snapshot.taken_at,
        start_charge_hour=ctx.start_charge_hour,
        needed=needed.value,
        needed_strict=round(MIN_SOC_PERCENT + soc_points(needed.dip_kwh), 1),
        needed_with_model=with_model.value,
        production=production.value if production is not None else None,
        variants=tuple(variants),
    )


def _score_models(
    actuals: DayActuals,
    snapshots: Sequence[ScoredSnapshot],
    earlier: Sequence[Mapping[int, float]],
    main: MomentReserve,
) -> tuple[ModelReserve, ...]:
    """Consumption models at 06:11: with ACTUAL PV (ranking) and production PV."""
    snapshot = consumed(
        snapshots, datetime.combine(actuals.day, MAIN_MOMENT.at, actuals.tz)
    )
    assert snapshot is not None  # `main` exists, so it was found
    ctx = _target_ctx(actuals.day, snapshot)
    views = _ActualViews(actuals, ctx)
    production_result = snapshot.result(PRODUCTION)
    out: list[ModelReserve] = []
    for model in ConsumptionModel:
        if model is ConsumptionModel.OLD_RULE:
            on_actual_pv = _old_rule(views, earlier)
            on_forecast = None
        else:
            profile = model.reserve_profile(earlier)
            if profile is None:
                continue
            on_actual_pv = views.target(views.model(profile)).value
            forecast = (
                target_soc_for(production_result, profile, ctx, is_today=True)
                if production_result is not None
                else None
            )
            on_forecast = forecast.value if forecast is not None else None
        out.append(
            ModelReserve(
                model=model.key,
                error_pp=main.needed - on_actual_pv,
                error_production_pv_pp=(
                    main.needed - on_forecast if on_forecast is not None else None
                ),
                earlier_days=len(earlier),
            )
        )
    return tuple(out)


def _old_rule(views: _ActualViews, earlier: Sequence[Mapping[int, float]]) -> int:
    """Max of flat and the earlier days without the worst (production until 14.10)."""
    flat = views.target(views.model(ConsumptionProfile.flat())).value
    per_day = [views.target(views.model(window_profile(day))).value for day in earlier]
    without_worst = T1SocReserve.prev_without_worst_of(per_day)
    return max(flat, without_worst) if without_worst is not None else flat


def soc_points(kwh: float) -> float:
    """SoC points a kWh of dip costs, conversion losses included."""
    return kwh / (BATTERY_CAPACITY_KWH / 100) * (1 + LOSS_FACTOR)


def _target_ctx(day: date, snapshot: ScoredSnapshot) -> TargetSocContext:
    """Rebuild the target-SOC inputs production had when it read this snapshot."""
    ctx = snapshot.context
    return TargetSocContext(
        target_date=day,
        signals=ctx.signals,
        live_consumption_w=ctx.consumption_w,
        start_charge_hour=ctx.start_charge_hour,
        now=ctx.now,
        now_in_window=ctx.now.hour in PV_WINDOW_HOURS and ctx.now.date() == day,
    )


class _ActualViews:
    """Actual PV and consumption seen from the moment, as production views a forecast."""

    def __init__(self, actuals: DayActuals, ctx: TargetSocContext) -> None:
        self._ctx = ctx
        now = ctx.now if ctx.now_in_window else None
        self._pv = PvProfile(buckets=actuals.window_buckets(PV, now))
        # Whole-window actual consumption, viewed from the moment like a model:
        # the in-progress bucket at live power on every side, so the PV part
        # isolates PV exactly.
        self.cons = ConsumptionProfile(buckets=actuals.window_buckets(CONSUMPTION))
        self._count_from = count_from(ctx, is_today=True)

    def model(self, profile: ConsumptionProfile) -> ConsumptionProfile:
        """View a consumption profile as production does at the moment."""
        return now_aware_consumption(
            profile, self._ctx, apply_now=self._ctx.now_in_window
        )

    def target(self, cons: ConsumptionProfile) -> TargetSocResult:
        """Target SOC of ACTUAL PV with `cons` (already a now-aware view)."""
        return _calculate_target_soc(
            self._pv,
            cons,
            start_charge_hour=self._ctx.start_charge_hour,
            count_from=self._count_from,
        )


# --- Value objects --- #


@dataclass(frozen=True)
class VariantReserve:
    """One PV forecast at one moment (SoC points; + = too little reserve)."""

    variant: str
    reserve: int  # what the forecast asked for, with the production model
    pv_error_pp: int  # PV part, production consumption model on both sides
    pv_error_actual_cons_pp: int  # the same with actual consumption on both sides

    @property
    def model_effect_pp(self) -> int:
        """How much the consumption model moves this PV score."""
        return self.pv_error_pp - self.pv_error_actual_cons_pp

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "reserve": self.reserve,
            "pv_error_pp": self.pv_error_pp,
            "pv_error_actual_cons_pp": self.pv_error_actual_cons_pp,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VariantReserve:
        return cls(
            variant=str(data["variant"]),
            reserve=int(data["reserve"]),
            pv_error_pp=int(data["pv_error_pp"]),
            pv_error_actual_cons_pp=int(data["pv_error_actual_cons_pp"]),
        )


@dataclass(frozen=True)
class MomentReserve:
    """The reserve at one moment: what was needed, what production asked, per variant."""

    label: str
    snapshot_at: datetime
    start_charge_hour: int | None
    needed: int  # actual PV + actual consumption, production formula (buffer incl.)
    needed_strict: float  # the same without the buffer: 10% + dip
    needed_with_model: int  # actual PV + production consumption model
    production: int | None  # production forecast + production consumption model
    variants: tuple[VariantReserve, ...]

    @property
    def production_error_pp(self) -> int | None:
        """Needed − asked: + = production kept too little."""
        return None if self.production is None else self.needed - self.production

    @property
    def pv_part_pp(self) -> int | None:
        return (
            None
            if self.production is None
            else self.needed_with_model - self.production
        )

    @property
    def consumption_part_pp(self) -> int:
        return self.needed - self.needed_with_model

    def variant(self, key: str) -> VariantReserve | None:
        return next((v for v in self.variants if v.variant == key), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "snapshot_at": self.snapshot_at.isoformat(),
            "start_charge_hour": self.start_charge_hour,
            "needed": self.needed,
            "needed_strict": self.needed_strict,
            "needed_with_model": self.needed_with_model,
            "production": self.production,
            "variants": [v.to_dict() for v in self.variants],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MomentReserve:
        return cls(
            label=str(data["label"]),
            snapshot_at=datetime.fromisoformat(data["snapshot_at"]),
            start_charge_hour=data.get("start_charge_hour"),
            needed=int(data["needed"]),
            needed_strict=float(data["needed_strict"]),
            needed_with_model=int(data["needed_with_model"]),
            production=data.get("production"),
            variants=tuple(VariantReserve.from_dict(v) for v in data["variants"]),
        )


@dataclass(frozen=True)
class ModelReserve:
    """One consumption model at 06:11 (SoC points; + = model too low)."""

    model: str
    error_pp: int  # with ACTUAL PV — the ranking
    error_production_pv_pp: int | None  # with the production PV forecast
    earlier_days: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "error_pp": self.error_pp,
            "error_production_pv_pp": self.error_production_pv_pp,
            "earlier_days": self.earlier_days,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ModelReserve:
        return cls(
            model=str(data["model"]),
            error_pp=int(data["error_pp"]),
            error_production_pv_pp=data.get("error_production_pv_pp"),
            earlier_days=int(data["earlier_days"]),
        )
