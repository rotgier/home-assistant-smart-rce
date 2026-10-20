"""Forecast scoreboard tables, ready for the dashboard card.

The card (`forecast-score-tables-card` in home-assistant-config `www/`) only
draws: every rule of presentation lives here, under test — labels, which
variant was the system's, the text of a cell, its severity (`warn` / `bad`)
and the key it sorts by. The card sorts by `sort` ascending on the first click
(smaller = better: an error's size, a count of bad verdicts, minus a count of
good ones), descending on the second.

Contract with the card, versioned by `TABLES_SCHEMA` (the card refuses
another):

    {"id", "title", "note", "row_header", "group",
     "columns": [{"key", "label", "sub"}],
     "rows": [{"key", "label", "system", "cells": {column: {"text", "sub",
               "severity", "sort"}}}],
     "sort": {"column", "descending"} | None, "sort_hint", "empty"}

Tables of one `group` share the sort the viewer picks (the moment columns of
the day: a click on 08:41 sorts every table that has it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from homeassistant.util import dt as dt_util

from ..domain.charge_start_shift import to_hhmm
from ..domain.forecast_score.day_score import Pending, ScoreStatus
from ..domain.forecast_score.production_setup import CURRENT

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from ..domain.forecast_score.day_score import DayScore
    from ..domain.forecast_score.so_far_score import MomentSoFar

TABLES_SCHEMA: Final = 1

VARIANT_LABELS: Final[dict[str, str]] = {
    "solcast_p50": "Solcast p50",
    "at_6": "At 6",
    "live": "Live",
    "live_soft_0.3": "Soft 0,3",
    "live_soft": "Soft 0,4",
    "live_soft_0.5": "Soft 0,5",
    "live_soft_0.6": "Soft 0,6",
    "extrapolated_live_pattern": "Extrap Pattern",
    "extrapolated_live_proportional": "Extrap Prop",
    "extrapolated_live_band": "Extrap Band",
    "extrapolated_live_band_recent": "Extrap Band R",
    "extrapolated_live_proportional_capped": "Extrap Prop Cap",
    "extrapolated_live_blend": "Extrap Blend",
}
MODEL_LABELS: Final[dict[str, str]] = {
    "flat": "flat 0,9",
    "p50": "p50 (mediana)",
    "p60": "p60",
    "p70": "p70",
    "p80": "p80",
    "p90": "p90",
    "old_rule": "stara reguła",
}
VERDICT_LABELS: Final[dict[str, str]] = {
    "on_time": "na czas",
    "too_early": "za wcześnie",
    "too_late": "za późno",
    "rightly_short": "słusznie „nie starczy”",
    "wrongly_short": "niesłusznie „nie starczy”",
}

# The day's moment columns share one sort.
MOMENTS_GROUP: Final = "day_moments"
# A forecast off by more than this share of what came (and this many kWh).
_WARN_SHARE: Final = 0.2
_WARN_KWH: Final = 0.3
_BAD_SHARE: Final = 0.5
_BAD_KWH: Final = 1.0
# A reserve error beyond these points (− = too little: the dangerous side).
_RESERVE_BAD_PP: Final = -3
_RESERVE_WARN_PP: Final = 3
_VERDICT_SEVERITY: Final[dict[str, str | None]] = {
    "on_time": None,
    "rightly_short": None,
    "too_early": "warn",
    "wrongly_short": "warn",
    "too_late": "bad",
}


def day_tables(score: DayScore, system_variant: str) -> list[dict[str, Any]]:
    """Tables of one day: PV accuracy (to 13:00 and 16:00), reserve, start."""
    cutoff = (
        f"do {dt_util.as_local(score.cutoff):%H:%M}"
        if score.cutoff is not None and score.status is ScoreStatus.PRELIMINARY
        else None
    )
    sort_column = _sort_moment(score)
    hint = (
        f"auto = chwila przed oddaniem po najwyższej porannej cenie "
        f"({score.discharge_peak_hour:02d}:00)"
        if score.discharge_peak_hour is not None
        else "brak porannego oddania — bez domyślnego sortowania"
    )
    return [
        _accuracy_table(
            "so_far",
            f"Trafność PV {cutoff or 'w oknie 07–13'} per wariant",
            "kWh, prognoza − rzeczywiste od chwili (najwcześniej 07:00);"
            " + = obiecała więcej",
            score.so_far,
            system_variant,
            sort_column,
            hint,
        ),
        _accuracy_table(
            "so_far_day",
            f"Trafność PV {cutoff or 'w oknie do 16:00'} per wariant — cały dzień"
            " ładowania",
            "kWh, prognoza − rzeczywiste od chwili (najwcześniej 07:00) do 16:00;"
            " + = obiecała więcej",
            score.so_far_day,
            system_variant,
            sort_column,
            hint,
        ),
        _reserve_table(score, system_variant, sort_column, hint),
        _start_table(score, system_variant, sort_column, hint),
    ]


def _sort_moment(score: DayScore) -> str | None:
    """Return the moment the dearest morning discharge read (last before its hour)."""
    hour = score.discharge_peak_hour
    if hour is None:
        return None
    before = [m.label for m in score.so_far if m.label < f"{hour:02d}:00"]
    return before[-1] if before else None


def _accuracy_table(  # noqa: PLR0913, PLR0917 — one table, its wording
    table_id: str,
    title: str,
    note: str,
    moments: Sequence[MomentSoFar],
    system_variant: str,
    sort_column: str | None,
    hint: str,
) -> dict[str, Any]:
    columns = [
        {
            "key": m.label,
            "label": m.label,
            "sub": f"{dt_util.as_local(m.since):%H:%M}–{dt_util.as_local(m.until):%H:%M}",
        }
        for m in moments
    ]

    def cell(variant: str, moment: MomentSoFar) -> dict[str, Any] | None:
        error = moment.pv_error_kwh(variant)
        if error is None:
            return None
        return _cell(
            _signed(error),
            severity=_accuracy_severity(error, moment.pv_actual_kwh),
            sort=abs(error),
        )

    return _table(
        table_id,
        title,
        note,
        columns,
        _variant_rows(
            moments,
            system_variant,
            lambda variant: {m.label: cell(variant, m) for m in moments},
        ),
        sort_column,
        hint,
        empty="brak danych po żadnej chwili",
    )


def _reserve_table(
    score: DayScore, system_variant: str, sort_column: str | None, hint: str
) -> dict[str, Any]:
    moments = score.reserve
    columns = [{"key": m.label, "label": m.label, "sub": None} for m in moments]

    def cells(variant: str) -> dict[str, dict[str, Any] | None]:
        out: dict[str, dict[str, Any] | None] = {}
        for moment in moments:
            found = next((v for v in moment.variants if v.variant == variant), None)
            out[moment.label] = (
                _cell(
                    _signed(found.pv_error_pp),
                    sub=_signed(found.pv_error_actual_cons_pp),
                    severity=_reserve_severity(found.pv_error_pp),
                    sort=abs(found.pv_error_pp),
                )
                if found is not None
                else None
            )
        return out

    waiting = Pending.RESERVE in score.pending
    return _table(
        "reserve",
        "Rezerwa — część PV błędu per wariant",
        "pp, − = za mało · z bieżącym modelem zużycia / mała liczba: przy"
        " rzeczywistym zużyciu",
        columns,
        _variant_rows(moments, system_variant, cells),
        sort_column,
        hint,
        empty=(
            "czeka do 13:06 — okno T1 jeszcze trwa"
            if waiting
            else "dzień wolny — bez T1"
        ),
    )


def _start_table(
    score: DayScore, system_variant: str, sort_column: str | None, hint: str
) -> dict[str, Any]:
    moments = score.start
    columns = [
        {
            "key": m.label,
            "label": f"★ {to_hhmm(m.minute)}" if m.label == "start" else m.label,
            "sub": None,
        }
        for m in moments
    ]

    def cells(variant: str) -> dict[str, dict[str, Any] | None]:
        out: dict[str, dict[str, Any] | None] = {}
        for moment in moments:
            found = next((v for v in moment.variants if v.variant == variant), None)
            out[moment.label] = (
                _cell(
                    to_hhmm(found.call.start),
                    sub=_signed(found.call.delta_h),
                    severity=_VERDICT_SEVERITY.get(str(found.call.verdict)),
                    sort=abs(found.call.delta_h),
                    title=VERDICT_LABELS.get(str(found.call.verdict)),
                )
                if found is not None
                else None
            )
        return out

    waiting = Pending.START in score.pending
    return _table(
        "start",
        "Start — warianty PV",
        "start, mała liczba: błąd h, + = za późno · z bieżącym modelem zużycia;"
        " optymalnie = rzeczywiste PV + ten sam model",
        columns,
        _variant_rows(moments, system_variant, cells),
        sort_column,
        hint,
        empty=(
            "czeka — pojawi się, gdy dalsze dane nie mogą już zmienić wyniku,"
            " najpóźniej w T2"
            if waiting
            else "brak oceny (brak cen RCE / S0)"
        ),
    )


def ranking_tables(ranking: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Tables of the 30-day ranking: reserve 06:11 and 08:41, start, models."""
    system = CURRENT.pv_variant
    reserve = ranking.get("reserve") or {}
    tables = [
        _ranking_reserve_table(reserve, label, system) for label in ("06:11", "08:41")
    ]
    tables.append(_ranking_start_table(ranking.get("start") or {}, system))
    tables.append(_ranking_models_table(ranking.get("reserve_models") or {}))
    tables.append(_ranking_start_models_table(ranking.get("start_models") or {}))
    return tables


def _ranking_reserve_table(
    reserve: Mapping[str, Any], label: str, system: str
) -> dict[str, Any]:
    rows_all = (reserve.get("all") or {}).get(label) or []
    by_kind = {
        kind: {r["variant"]: r for r in (reserve.get(kind) or {}).get(label) or []}
        for kind in ("cloudy", "sunny")
    }
    columns = _columns(
        ("n", "n"),
        ("mae", "MAE"),
        ("bias", "bias"),
        ("short_3", "< −3"),
        ("short_5", "< −5"),
        ("asym", "strata asym.*"),
        ("cloudy", "☁ MAE"),
        ("sunny", "☀ MAE"),
        ("vs_p50", "vs p50 W/R/P"),
    )
    rows = [
        {
            "key": row["variant"],
            "label": _variant_label(row["variant"], system),
            "system": row["variant"] == system,
            "cells": {
                "n": _cell(str(row["n"]), sort=-row["n"]),
                "mae": _cell(_number(row["mae"]), sort=row["mae"], strong=True),
                "bias": _cell(
                    _signed(row["bias"]),
                    severity=_reserve_severity(row["bias"]),
                    sort=abs(row["bias"]),
                ),
                "short_3": _cell(str(row["short_3"]), sort=row["short_3"]),
                "short_5": _cell(str(row["short_5"]), sort=row["short_5"]),
                "asym": _cell(_number(row["asym_loss"]), sort=row["asym_loss"]),
                "cloudy": _kind_cell(by_kind["cloudy"].get(row["variant"])),
                "sunny": _kind_cell(by_kind["sunny"].get(row["variant"])),
                "vs_p50": _cell(
                    f"{row['wins']}/{row['ties']}/{row['losses']}",
                    sort=row["losses"] - row["wins"],
                ),
            },
        }
        for row in rows_all
    ]
    return _table(
        f"ranking_reserve_{label}",
        f"Rezerwa {label}{' (linia rozładowania)' if label == '08:41' else ''}",
        "część PV [pp, − = za mało] · MAE / bias",
        columns,
        rows,
        "mae",
        None,
        empty="brak dni z oceną",
        group=None,
    )


def _kind_cell(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return _cell(_number(row["mae"]), sort=row["mae"]) if row else None


def _ranking_start_table(start: Mapping[str, Any], system: str) -> dict[str, Any]:
    rows_all = (start.get("all") or {}).get("start") or []
    columns = _columns(
        ("n", "n"),
        ("on_time", "na czas"),
        ("too_late", "za późno"),
        ("too_early", "za wcześnie"),
        ("wrongly_short", "niesł. „nie st.”"),
        ("mae_h", "MAE h"),
        ("short_kwh", "niedobór kWh"),
        ("early_kwh", "za wcześnie kWh"),
        ("vs_p50", "vs p50 W/R/P"),
    )
    rows = [
        {
            "key": row["variant"],
            "label": _variant_label(row["variant"], system),
            "system": row["variant"] == system,
            "cells": {
                "n": _cell(str(row["n"]), sort=-row["n"]),
                **_verdict_cells(row["verdicts"]),
                "mae_h": _cell(_number(row["mae_h"]), sort=row["mae_h"], strong=True),
                "short_kwh": _cell(_number(row["short_kwh"]), sort=row["short_kwh"]),
                "early_kwh": _cell(_number(row["early_kwh"]), sort=row["early_kwh"]),
                "vs_p50": _cell(
                    f"{row['wins']}/{row['ties']}/{row['losses']}",
                    sort=row["losses"] - row["wins"],
                ),
            },
        }
        for row in rows_all
    ]
    return _table(
        "ranking_start",
        "Start — ostatnia ocena przed startem",
        "+ = za późno · liczba werdyktów",
        columns,
        rows,
        "mae_h",
        None,
        empty="brak dni z oceną",
        group=None,
    )


def _verdict_cells(verdicts: Mapping[str, int]) -> dict[str, dict[str, Any] | None]:
    """Verdict counts: the good one sorts by its minus (more = better)."""
    on_time = verdicts.get("on_time", 0)
    return {
        "on_time": _cell(str(on_time), sort=-on_time),
        **{
            key: _cell(
                str(count := verdicts.get(key, 0)),
                severity=_VERDICT_SEVERITY[key] if count else None,
                sort=count,
            )
            for key in ("too_late", "too_early", "wrongly_short")
        },
    }


def _ranking_models_table(models: Mapping[str, Any]) -> dict[str, Any]:
    rows_all = models.get("all") or []
    columns = _columns(
        ("n", "n"),
        ("mae", "MAE"),
        ("bias", "bias"),
        ("short_3", "< −3"),
        ("short_5", "< −5"),
        ("production_pv", "z prognozą PV systemu: MAE / bias"),
    )
    system = CURRENT.reserve_model.key
    rows = [
        {
            "key": row["model"],
            "label": _model_label(row["model"], system),
            "system": row["model"] == system,
            "cells": {
                "n": _cell(str(row["n"]), sort=-row["n"]),
                "mae": _cell(_number(row["mae"]), sort=row["mae"], strong=True),
                "bias": _cell(
                    _signed(row["bias"]),
                    severity=_reserve_severity(row["bias"]),
                    sort=abs(row["bias"]),
                ),
                "short_3": _cell(str(row["short_3"]), sort=row["short_3"]),
                "short_5": _cell(str(row["short_5"]), sort=row["short_5"]),
                "production_pv": _cell(
                    f"{_number(row['mae_production_pv'])} /"
                    f" {_signed(row['bias_production_pv'])}",
                    sort=row["mae_production_pv"],
                ),
            },
        }
        for row in rows_all
    ]
    return _table(
        "ranking_models",
        "Modele zużycia — rezerwa 06:11",
        "rzeczywiste PV [pp] · − = model zużycia za niski (za mało rezerwy)",
        columns,
        rows,
        "mae",
        None,
        empty="brak dni z oceną",
        group=None,
        row_header="model zużycia",
    )


def _ranking_start_models_table(models: Mapping[str, Any]) -> dict[str, Any]:
    rows_all = (models.get("all") or {}).get("start") or []
    columns = _columns(
        ("n", "n"),
        ("on_time", "na czas"),
        ("too_late", "za późno"),
        ("too_early", "za wcześnie"),
        ("short_kwh", "niedobór kWh"),
    )
    system = CURRENT.start_model.key
    rows = [
        {
            "key": row["model"],
            "label": _model_label(row["model"], system),
            "system": row["model"] == system,
            "cells": {
                "n": _cell(str(row["n"]), sort=-row["n"]),
                **{
                    k: v
                    for k, v in _verdict_cells(row["verdicts"]).items()
                    if k != "wrongly_short"
                },
                "short_kwh": _cell(_number(row["short_kwh"]), sort=row["short_kwh"]),
            },
        }
        for row in rows_all
    ]
    return _table(
        "ranking_start_models",
        "Modele zużycia — start",
        "rzeczywiste PV, ostatnia ocena przed startem",
        columns,
        rows,
        None,
        None,
        empty="brak dni z oceną",
        group=None,
        row_header="model zużycia",
    )


# --- common helpers --- #


def _table(  # noqa: PLR0913, PLR0917 — the contract fields
    table_id: str,
    title: str,
    note: str,
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    sort_column: str | None,
    hint: str | None,
    *,
    empty: str,
    group: str | None = MOMENTS_GROUP,
    row_header: str = "wariant",
) -> dict[str, Any]:
    keys = {c["key"] for c in columns}
    return {
        "id": table_id,
        "title": title,
        "note": note,
        "row_header": row_header,
        "group": group,
        "columns": columns,
        "rows": rows,
        "sort": (
            {"column": sort_column, "descending": False}
            if sort_column in keys
            else None
        ),
        "sort_hint": hint,
        "empty": empty,
    }


def _variant_rows(
    moments: Sequence[Any],
    system_variant: str,
    cells: Callable[[str], dict[str, dict[str, Any] | None]],
) -> list[dict[str, Any]]:
    """One row per variant in the dashboard's order; none without moments."""
    if not moments:
        return []
    return [
        {
            "key": variant,
            "label": _variant_label(variant, system_variant),
            "system": variant == system_variant,
            "cells": cells(variant),
        }
        for variant in VARIANT_LABELS
    ]


def _columns(*pairs: tuple[str, str]) -> list[dict[str, Any]]:
    return [{"key": key, "label": label, "sub": None} for key, label in pairs]


def _cell(  # noqa: PLR0913 — the contract's fields
    text: str,
    *,
    sub: str | None = None,
    severity: str | None = None,
    sort: float | None = None,
    strong: bool = False,
    title: str | None = None,
) -> dict[str, Any]:
    return {
        "text": text,
        "sub": sub,
        "severity": severity,
        "sort": sort,
        "strong": strong,
        "title": title,
    }


def _variant_label(variant: str, system: str) -> str:
    label = VARIANT_LABELS.get(variant, variant)
    return f"{label} (system)" if variant == system else label


def _model_label(model: str, system: str) -> str:
    label = MODEL_LABELS.get(model, model)
    return f"{label} (system)" if model == system else label


def _accuracy_severity(error: float, actual: float) -> str | None:
    """Flag a forecast off by a large share of what came, and by a real amount."""
    if actual <= 0:
        return None
    share = abs(error) / actual
    if abs(error) >= _BAD_KWH and share > _BAD_SHARE:
        return "bad"
    if abs(error) >= _WARN_KWH and share > _WARN_SHARE:
        return "warn"
    return None


def _reserve_severity(error: float) -> str | None:
    """Flag too little reserve as dangerous; too much only defers PV."""
    if error < _RESERVE_BAD_PP:
        return "bad"
    if error > _RESERVE_WARN_PP:
        return "warn"
    return None


def _signed(value: float) -> str:
    return f"{value:+d}" if isinstance(value, int) else f"{value:+.2f}"


def _number(value: float | None) -> str:
    if value is None:
        return "—"
    return (
        str(value) if isinstance(value, int) else f"{value:.2f}".rstrip("0").rstrip(".")
    )
