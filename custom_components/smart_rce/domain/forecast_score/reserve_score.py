"""D1 — the T1 reserve: how much SoC to keep for 07:00-13:00, scored per moment.

Moments: 06:11 (the main one — first Solcast fetch of the day, the morning
charge has its target), 06:41 (last chance to charge less), and 07:41 / 08:41
/ 09:41 — the reserve for the REST of the window, the line a morning discharge
must not cross. Before 07:00 the whole window counts; inside it production
reads now-aware views (closed buckets 0, the in-progress one from live power),
and so does the score (`target_soc_for`, the path `TargetSoc` takes).

Each moment reads the snapshot the decision consumed. Signs (since 15.10.2026,
user's choice): error = what the system asked for − what was really needed, in
SoC points; − = the system asked for TOO LITTLE (the battery could run short —
the dangerous side), + = too much. The charge start uses the same "system −
benchmark" convention (there + = too late is the dangerous side).

The production error splits exactly into two parts:
- PV part  = R(forecast PV, model) − R(actual PV, model) — the variant ranking;
- consumption part = R(actual PV, model) − R(actual PV, actual cons);
where `model` is production's consumption rule. The ranking uses today's setup
(p60 of earlier workdays); the facts panel the setup the day ran on
(`production_setup`: the old max-of rule and buffer 12 until 14.10).
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
from ..pv_forecast import PvForecast
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
from .consumption_models import ConsumptionModel, window_profile
from .day_actuals import CONSUMPTION, PV
from .production_setup import CURRENT, MarginRule, ProductionSetup
from .variant_results import SCORED_VARIANTS, consumed

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from ..pv_forecast import PvForecastResult
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
    setup: ProductionSetup = CURRENT,
) -> tuple[tuple[MomentReserve, ...], tuple[ModelReserve, ...]]:
    """Every reserve moment of the day, and the consumption models at 06:11.

    Variants are ranked with today's setup (`CURRENT`); `setup` is the one
    production ran on that day — the facts panel.
    """
    ranking_rule = _Rule(
        CURRENT.reserve_model, earlier_workdays, CURRENT.buffer_percent
    )
    production_rule = _Rule(setup.reserve_model, earlier_workdays, setup.buffer_percent)
    moments: list[MomentReserve] = []
    for moment in RESERVE_MOMENTS:
        snapshot = consumed(
            snapshots, datetime.combine(actuals.day, moment.at, actuals.tz)
        )
        if snapshot is not None:
            moments.append(
                _score_moment(
                    moment, snapshot, actuals, ranking_rule, setup, production_rule
                )
            )
    main = next((m for m in moments if m.label == MAIN_MOMENT.label), None)
    models = (
        _score_models(actuals, snapshots, earlier_workdays, main)
        if main is not None
        else ()
    )
    return tuple(moments), models


def _score_moment(  # noqa: PLR0917 — one caller, the moment's whole world
    moment: ReserveMoment,
    snapshot: ScoredSnapshot,
    actuals: DayActuals,
    rule: _Rule,
    setup: ProductionSetup,
    production_rule: _Rule,
) -> MomentReserve:
    ctx = _target_ctx(actuals.day, snapshot)
    views = _ActualViews(actuals, ctx)
    needed = views.target(views.model(views.cons), rule.buffer)
    with_model = rule.on_actual(views)
    variants: list[VariantReserve] = []
    for variant in SCORED_VARIANTS:
        result = snapshot.result(variant)
        if result is None:
            continue
        on_model = rule.on_forecast(result, ctx)
        on_actual = target_soc_for(
            result, views.cons, ctx, is_today=True, buffer_percent=rule.buffer
        )
        if on_model is None or on_actual is None:
            continue
        variants.append(
            VariantReserve(
                variant=variant,
                reserve=on_model,
                pv_error_pp=on_model - with_model,
                pv_error_actual_cons_pp=on_actual.value - needed.value,
            )
        )
    return MomentReserve(
        label=moment.label,
        snapshot_at=snapshot.taken_at,
        start_charge_hour=ctx.start_charge_hour,
        needed=needed.value,
        needed_strict=round(MIN_SOC_PERCENT + soc_points(needed.dip_kwh), 1),
        needed_with_model=with_model,
        variants=tuple(variants),
        production=_production(snapshot, ctx, views, setup, production_rule),
    )


def _production(
    snapshot: ScoredSnapshot,
    ctx: TargetSocContext,
    views: _ActualViews,
    setup: ProductionSetup,
    rule: _Rule,
) -> ProductionReserve | None:
    """Score what production asked for that day, with that day's setup.

    With a margin setup the reserve carries production's own time-of-day
    margin (`T1SocReserve.margin_pp_at`) at the moment, capped at 100 as the
    sensor caps it.
    """
    result = snapshot.result(setup.pv_variant)
    reading = rule.on_forecast(result, ctx) if result is not None else None
    if reading is None:
        return None
    margin = _margin(snapshot, ctx, setup, rule, reading)
    return ProductionReserve(
        setup=setup.label,
        needed=views.target(views.model(views.cons), rule.buffer).value,
        needed_with_model=rule.on_actual(views),
        asked=min(100, reading + margin),
        asked_without_margin=min(100, reading),
        margin_pp=margin,
    )


def _margin(
    snapshot: ScoredSnapshot,
    ctx: TargetSocContext,
    setup: ProductionSetup,
    rule: _Rule,
    reading: int,
) -> int:
    """Production's own margin at the moment — the same functions it calls.

    The caps are readings of the capping variants on the same rule (p60,
    remaining window, buffer); a variant without a result does not count.
    """
    day, now = ctx.target_date, ctx.now
    if setup.margin is MarginRule.NONE:
        return 0
    if setup.margin is MarginRule.STEPS:
        return T1SocReserve.margin_pp_at(day, now)
    if setup.margin is MarginRule.LIVE_CAPS_0809:
        # a5b711a capped only 08-09, by Live; its 07-08 step stays whole.
        if now.date() == day and now.hour == 7:
            return T1SocReserve.margin_pp_at(day, now)
        return T1SocReserve.margin_pp_for(
            day, now, reading, _caps(snapshot, ctx, rule, (PvForecast.LIVE,))
        )
    return T1SocReserve.margin_pp_for(
        day, now, reading, _caps(snapshot, ctx, rule, _CAPPING_VARIANTS)
    )


_CAPPING_VARIANTS: Final = (
    PvForecast.LIVE,
    PvForecast.EXTRAP_PROPORTIONAL,
    PvForecast.EXTRAP_BLEND,
)


def _caps(
    snapshot: ScoredSnapshot,
    ctx: TargetSocContext,
    rule: _Rule,
    variants: Sequence[PvForecast],
) -> dict[str, int | None]:
    """Read the capping variants on the same rule, keyed by `PvForecast.key`."""
    caps: dict[str, int | None] = {}
    for variant in variants:
        result = snapshot.result(variant.key)
        caps[variant.key] = (
            rule.on_forecast(result, ctx) if result is not None else None
        )
    return caps


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
    production_result = snapshot.result(CURRENT.pv_variant)
    out: list[ModelReserve] = []
    for model in ConsumptionModel:
        if model.is_profile and model.reserve_profile(earlier) is None:
            continue
        rule = _Rule(model, earlier, CURRENT.buffer_percent)
        on_forecast = (
            rule.on_forecast(production_result, ctx)
            if production_result is not None
            else None
        )
        out.append(
            ModelReserve(
                model=model.key,
                error_pp=rule.on_actual(views) - main.needed,
                error_production_pv_pp=(
                    on_forecast - main.needed if on_forecast is not None else None
                ),
                earlier_days=len(earlier),
            )
        )
    return tuple(out)


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


@dataclass(frozen=True)
class _Rule:
    """A reserve rule: consumption model (or the old max-of rule) and buffer."""

    model: ConsumptionModel
    earlier: Sequence[Mapping[int, float]]
    buffer: int

    def on_actual(self, views: _ActualViews) -> int:
        """Return the reserve this rule asks for with ACTUAL PV."""
        return self._apply(
            lambda cons: views.target(views.model(cons), self.buffer).value
        )

    def on_forecast(
        self, result: PvForecastResult, ctx: TargetSocContext
    ) -> int | None:
        """Return the reserve this rule asks for with a forecast (None: none)."""

        def target(cons: ConsumptionProfile) -> int:
            found = target_soc_for(
                result, cons, ctx, is_today=True, buffer_percent=self.buffer
            )
            if found is None:
                raise _NoForecast
            return found.value

        try:
            return self._apply(target)
        except _NoForecast:
            return None

    def _apply(self, target: Callable[[ConsumptionProfile], int]) -> int:
        if self.model is ConsumptionModel.OLD_RULE:
            # Max of flat and the earlier days without the worst (until 14.10).
            flat = target(ConsumptionProfile.flat())
            per_day = [target(window_profile(day)) for day in self.earlier]
            without_worst = T1SocReserve.prev_without_worst_of(per_day)
            return max(flat, without_worst) if without_worst is not None else flat
        # Production falls back to flat without history.
        profile = self.model.reserve_profile(self.earlier) or ConsumptionProfile.flat()
        return target(profile)


class _NoForecast(Exception):
    """The forecast cannot give a target (does not reach the day, no live signal)."""


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

    def target(self, cons: ConsumptionProfile, buffer: int) -> TargetSocResult:
        """Target SOC of ACTUAL PV with `cons` (already a now-aware view)."""
        return _calculate_target_soc(
            self._pv,
            cons,
            start_charge_hour=self._ctx.start_charge_hour,
            count_from=self._count_from,
            buffer_percent=buffer,
        )


# --- Value objects --- #


@dataclass(frozen=True)
class VariantReserve:
    """One PV forecast at one moment (SoC points; − = too little reserve)."""

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
    """The reserve at one moment: what was needed, per variant, what production asked.

    `needed` / `needed_with_model` / variants use today's setup (the ranking);
    `production` the setup of the day (the facts).
    """

    label: str
    snapshot_at: datetime
    start_charge_hour: int | None
    needed: int  # actual PV + actual consumption, production formula (buffer incl.)
    needed_strict: float  # the same without the buffer: 10% + dip
    needed_with_model: int  # actual PV + today's consumption model
    variants: tuple[VariantReserve, ...]
    production: ProductionReserve | None

    @property
    def production_error_pp(self) -> int | None:
        return self.production.error_pp if self.production is not None else None

    @property
    def pv_part_pp(self) -> int | None:
        return self.production.pv_part_pp if self.production is not None else None

    @property
    def consumption_part_pp(self) -> int | None:
        return (
            self.production.consumption_part_pp if self.production is not None else None
        )

    @property
    def margin_part_pp(self) -> int | None:
        return self.production.margin_part_pp if self.production is not None else None

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
            "variants": [v.to_dict() for v in self.variants],
            "production": (
                self.production.to_dict() if self.production is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MomentReserve:
        raw = data.get("production")
        return cls(
            label=str(data["label"]),
            snapshot_at=datetime.fromisoformat(data["snapshot_at"]),
            start_charge_hour=data.get("start_charge_hour"),
            needed=int(data["needed"]),
            needed_strict=float(data["needed_strict"]),
            needed_with_model=int(data["needed_with_model"]),
            variants=tuple(VariantReserve.from_dict(v) for v in data["variants"]),
            production=(
                ProductionReserve.from_dict(raw) if isinstance(raw, dict) else None
            ),
        )


@dataclass(frozen=True)
class ProductionReserve:
    """What production asked for with the setup of the day (SoC points).

    error = asked − needed = PV part (asked without margin − needed_with_model)
    + consumption part (needed_with_model − needed) + margin part (what the
    time-of-day margin added); − = the system asked for too little.
    """

    setup: str
    needed: int  # actual PV + actual consumption, the day's buffer
    needed_with_model: int  # actual PV + the day's consumption rule
    asked: int  # the day's forecast + consumption rule + margin (max 100)
    asked_without_margin: int | None = None  # None = no margin (older records)
    margin_pp: int = 0  # nominal margin in force at the moment

    @property
    def reading(self) -> int:
        """What the forecast and the rule asked for before the margin."""
        if self.asked_without_margin is None:
            return self.asked
        return self.asked_without_margin

    @property
    def error_pp(self) -> int:
        return self.asked - self.needed

    @property
    def pv_part_pp(self) -> int:
        return self.reading - self.needed_with_model

    @property
    def consumption_part_pp(self) -> int:
        return self.needed_with_model - self.needed

    @property
    def margin_part_pp(self) -> int:
        """What the margin really added (less than nominal when capped at 100)."""
        return self.asked - self.reading

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup": self.setup,
            "needed": self.needed,
            "needed_with_model": self.needed_with_model,
            "asked": self.asked,
            "asked_without_margin": self.asked_without_margin,
            "margin_pp": self.margin_pp,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProductionReserve:
        return cls(
            setup=str(data["setup"]),
            needed=int(data["needed"]),
            needed_with_model=int(data["needed_with_model"]),
            asked=int(data["asked"]),
            asked_without_margin=data.get("asked_without_margin"),
            margin_pp=int(data.get("margin_pp", 0)),
        )


@dataclass(frozen=True)
class ModelReserve:
    """One consumption model at 06:11 (SoC points; − = model too low)."""

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
