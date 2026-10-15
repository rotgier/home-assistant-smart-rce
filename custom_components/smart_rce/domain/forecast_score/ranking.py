"""Ranking — final day scores folded into per-variant and per-model tables.

Aggregates only mean something over many days (a trustworthy ranking from ~30);
a single day is read on its card, not here. Every table is split by day kind
(all / cloudy / sunny), since a forecast that wins sunny days can lose cloudy ones.

Per row:
- D1 (SoC points, system − needed; − = too little reserve): n, MAE, bias,
  days < −3 / < −5 ("dangerous": the battery would run short), asymmetric loss, and against
  raw Solcast p50 on the same days — MAE advantage, wins / ties / losses
  (a tie: |error| within TIE_PP of p50's).
- D2 (hours, + = too late): n, verdict counts, MAE of Δ, kWh short when late,
  kWh stored too early, wins / ties / losses against p50 (tie: within TIE_H).

The asymmetric loss weighs a short reserve more than a surplus one: running
short buys T1 energy, keeping too much only defers PV. The weight is
provisional — the real cost depends on the day's prices (open question).
"""

from __future__ import annotations

from collections import Counter
import statistics
from typing import TYPE_CHECKING, Any, Final

from .day_type import DayKind
from .reserve_score import RESERVE_MOMENTS
from .start_score import PREVIEW_MOMENTS, START_MOMENT, StartVerdict
from .variant_results import RAW_SOLCAST

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from .day_score import DayScore

TIE_PP: Final = 3
TIE_H: Final = 1.0
DANGER_PP: Final = (3, 5)
SHORT_WEIGHT: Final = 2.0  # asymmetric loss: weight of a negative (short) error
_KINDS: Final = ("all", DayKind.CLOUDY.value, DayKind.SUNNY.value)
_ALL_RESERVE: Final = tuple(m.label for m in RESERVE_MOMENTS)
_ALL_START: Final = (*(label for label, _ in PREVIEW_MOMENTS), START_MOMENT)
# Per day kind only the moments that matter most — keeps the attributes small.
_KEY_RESERVE: Final = ("06:11", "08:41")
_KEY_START: Final = ("08:41", START_MOMENT)


def rank(scores: Sequence[DayScore]) -> dict[str, Any]:
    """Build the ranking tables of `scores` (final ones, any order)."""
    days = sorted(scores, key=lambda s: s.day)
    return {
        "days": [s.day.isoformat() for s in days],
        "day_kinds": {
            s.day.isoformat(): (s.day_type.kind.value if s.day_type.kind else None)
            for s in days
        },
        "reserve": {
            kind: _reserve_tables(
                _of_kind(days, kind), _ALL_RESERVE if kind == "all" else _KEY_RESERVE
            )
            for kind in _KINDS
        },
        "reserve_models": {
            kind: _reserve_model_table(_of_kind(days, kind)) for kind in _KINDS
        },
        "start": {
            kind: _start_tables(
                _of_kind(days, kind), _ALL_START if kind == "all" else _KEY_START
            )
            for kind in _KINDS
        },
        "start_models": {
            kind: _start_model_table(
                _of_kind(days, kind), _ALL_START if kind == "all" else _KEY_START
            )
            for kind in _KINDS
        },
        "production": _production(days),
    }


def _of_kind(days: Sequence[DayScore], kind: str) -> list[DayScore]:
    if kind == "all":
        return list(days)
    return [d for d in days if d.day_type.kind is not None and d.day_type.kind == kind]


# --- D1 --- #


def _reserve_tables(
    days: Sequence[DayScore], labels: tuple[str, ...]
) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {}
    for label in labels:
        per_variant: dict[str, dict[str, tuple[int, int]]] = {}
        for day in days:
            scored = day.reserve_moment(label)
            if scored is None:
                continue
            for v in scored.variants:
                per_variant.setdefault(v.variant, {})[day.day.isoformat()] = (
                    v.pv_error_pp,
                    v.pv_error_actual_cons_pp,
                )
        if per_variant:
            baseline = {k: e for k, (e, _a) in per_variant.get(RAW_SOLCAST, {}).items()}
            tables[label] = [
                _reserve_row(variant, errors, baseline)
                for variant, errors in per_variant.items()
            ]
    return tables


def _reserve_row(
    variant: str, errors: dict[str, tuple[int, int]], baseline: dict[str, int]
) -> dict[str, Any]:
    main = [e for e, _a in errors.values()]
    actual = [a for _e, a in errors.values()]
    row = {"variant": variant, **_pp_stats(main)}
    row["mae_actual_cons"] = _mean(map(abs, actual))
    row["bias_actual_cons"] = _mean(actual)
    row |= _versus(
        {k: e for k, (e, _a) in errors.items()}, baseline, lambda e: abs(e), TIE_PP
    )
    return row


def _reserve_model_table(days: Sequence[DayScore]) -> list[dict[str, Any]]:
    per_model: dict[str, list[int]] = {}
    per_model_fc: dict[str, list[int]] = {}
    for day in days:
        for m in day.reserve_models:
            per_model.setdefault(m.model, []).append(m.error_pp)
            if m.error_production_pv_pp is not None:
                per_model_fc.setdefault(m.model, []).append(m.error_production_pv_pp)
    return [
        {
            "model": model,
            **_pp_stats(errors),
            "mae_production_pv": _mean(map(abs, per_model_fc.get(model, []))),
            "bias_production_pv": _mean(per_model_fc.get(model, [])),
        }
        for model, errors in per_model.items()
    ]


def _pp_stats(errors: Sequence[int]) -> dict[str, Any]:
    return {
        "n": len(errors),
        "mae": _mean(map(abs, errors)),
        "bias": _mean(errors),
        **{f"short_{t}": sum(e < -t for e in errors) for t in DANGER_PP},
        "asym_loss": _mean(-SHORT_WEIGHT * e if e < 0 else e for e in errors),
    }


# --- D2 --- #


def _start_tables(
    days: Sequence[DayScore], labels: tuple[str, ...]
) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {}
    for label in labels:
        per_variant: dict[str, dict[str, Any]] = {}
        for day in days:
            scored = day.start_moment(label)
            if scored is None:
                continue
            for v in scored.variants:
                per_variant.setdefault(v.variant, {})[day.day.isoformat()] = v
        if per_variant:
            baseline = {
                k: v.call.delta_h for k, v in per_variant.get(RAW_SOLCAST, {}).items()
            }
            tables[label] = [
                {
                    "variant": variant,
                    **_calls_stats([v.call for v in calls.values()]),
                    "verdicts_actual_cons": dict(
                        Counter(str(v.call_actual_cons.verdict) for v in calls.values())
                    ),
                    **_versus(
                        {k: v.call.delta_h for k, v in calls.items()},
                        baseline,
                        abs,
                        TIE_H,
                    ),
                }
                for variant, calls in per_variant.items()
            ]
    return tables


def _start_model_table(
    days: Sequence[DayScore], labels: tuple[str, ...]
) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {}
    for label in labels:
        per_model: dict[str, list[Any]] = {}
        for day in days:
            scored = day.start_moment(label)
            if scored is None:
                continue
            for m in scored.models:
                per_model.setdefault(m.model, []).append(m)
        if per_model:
            tables[label] = [
                {
                    "model": model,
                    **_calls_stats([m.call for m in calls]),
                    "verdicts_production_pv": dict(
                        Counter(
                            str(m.call_production_pv.verdict)
                            for m in calls
                            if m.call_production_pv is not None
                        )
                    ),
                }
                for model, calls in per_model.items()
            ]
    return tables


def _calls_stats(calls: Sequence[Any]) -> dict[str, Any]:
    return {
        "n": len(calls),
        "verdicts": dict(Counter(str(c.verdict) for c in calls)),
        "mae_h": _mean(abs(c.delta_h) for c in calls),
        "short_kwh": round(sum(c.shortfall_kwh for c in calls), 2),
        "early_kwh": round(sum(c.early_kwh for c in calls), 2),
        "late": sum(c.verdict is StartVerdict.TOO_LATE for c in calls),
    }


# --- production --- #


def _production(days: Sequence[DayScore]) -> dict[str, Any]:
    reserve = [d.reserve_error_pp for d in days if d.reserve_error_pp is not None]
    pv = [d.reserve_error_pv_pp for d in days if d.reserve_error_pv_pp is not None]
    cons = [c for d in days if (c := d.reserve_error_consumption_pp) is not None]
    starts = [d.start_delta_h for d in days if d.start_delta_h is not None]
    imports = [
        d.outcome.t1_import_kwh for d in days if d.outcome.t1_import_kwh is not None
    ]
    return {
        "reserve": _pp_stats(reserve) if reserve else None,
        "reserve_pv_part": _mean(pv),
        "reserve_consumption_part": _mean(cons),
        "start_mae_h": _mean(map(abs, starts)),
        "start_bias_h": _mean(starts),
        "t1_import_kwh": round(sum(imports), 2),
        "days_with_t1_import": sum(i > 0.05 for i in imports),
    }


# --- common helpers --- #


def _versus(
    errors: Mapping[str, float],
    baseline: Mapping[str, float],
    size: Callable[[float], float],
    tie: float,
) -> dict[str, Any]:
    """Against p50 on the days both have: MAE advantage, wins / ties / losses."""
    shared = [k for k in errors if k in baseline]
    if not shared:
        return {"vs_p50_mae": None, "wins": 0, "ties": 0, "losses": 0}
    gaps = [size(baseline[k]) - size(errors[k]) for k in shared]
    return {
        "vs_p50_mae": _mean(gaps),
        "wins": sum(g >= tie for g in gaps),
        "ties": sum(-tie < g < tie for g in gaps),
        "losses": sum(g <= -tie for g in gaps),
    }


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return round(statistics.fmean(items), 2) if items else None
