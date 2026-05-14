"""Tests for `target_soc_matrix.compute_matrix`.

Verifies that the matrix delegates each cell to `calculate_target_soc`
(parity) and that row/column summaries match the supplied profiles.
"""

from __future__ import annotations

from custom_components.smart_rce.domain.pv_forecast import ConsumptionProfile, PvProfile
from custom_components.smart_rce.domain.target_soc import (
    MIN_SOC_PERCENT,
    calculate_target_soc,
)
from custom_components.smart_rce.domain.target_soc_matrix import (
    ConsLabel,
    compute_matrix,
)

_BUCKET_KEYS = [(7 + idx // 2, (idx % 2) * 30) for idx in range(12)]


def _pv(value: float) -> PvProfile:
    return PvProfile(buckets={k: value for k in _BUCKET_KEYS})


def _cons(value: float) -> ConsumptionProfile:
    return ConsumptionProfile(buckets={k: value for k in _BUCKET_KEYS})


def _pv_list(values: list[float]) -> PvProfile:
    return PvProfile(buckets={_BUCKET_KEYS[idx]: values[idx] for idx in range(12)})


def _cons_list(values: list[float]) -> ConsumptionProfile:
    return ConsumptionProfile(
        buckets={_BUCKET_KEYS[idx]: values[idx] for idx in range(12)}
    )


_PV_GENEROUS = _pv(1.0)  # 1 kWh / 30min → 12 kWh in the window
_PV_DEFICIT = _pv(0.15)  # 0.15 kWh / 30min → 1.8 kWh in the window
_CONS_BASE = _cons(0.45)
_CONS_HIGH = _cons(0.8)


def test_minimal_2x2_matrix_returns_cells_and_sums() -> None:
    matrix = compute_matrix(
        pv_profiles_by_strategy={"live": _PV_GENEROUS, "at_6": _PV_DEFICIT},
        cons_profiles_by_strategy={"live_cons": _CONS_BASE, "prev_1": _CONS_HIGH},
        cons_labels={"prev_1": ConsLabel(key="prev_1", weekday="Mon")},
        source_day_pv_sums={"live_cons": None, "prev_1": 6.5},
        start_charge_hour=None,
    )

    assert matrix.pv_strategies == ("live", "at_6")
    assert len(matrix.cons_strategies) == 2
    cons_by_key = {c.key: c for c in matrix.cons_strategies}
    assert cons_by_key["prev_1"].weekday == "Mon"
    assert cons_by_key["live_cons"].weekday is None

    # Generous PV with baseline cons → no deficit → MIN_SOC.
    assert matrix.cells_pct[("live", "live_cons")] == MIN_SOC_PERCENT
    # Deficit PV → SOC > MIN.
    assert matrix.cells_pct[("at_6", "live_cons")] > MIN_SOC_PERCENT
    # Higher cons makes it worse.
    assert (
        matrix.cells_pct[("at_6", "prev_1")] >= matrix.cells_pct[("at_6", "live_cons")]
    )

    # Row / column sums.
    assert matrix.pv_sums_kwh == {"live": 12.0, "at_6": 1.8}
    assert matrix.cons_sums_kwh == {"live_cons": 5.4, "prev_1": 9.6}
    # Source-day PV passthrough preserved (and includes None for Live).
    assert matrix.source_day_pv_sums_kwh == {"live_cons": None, "prev_1": 6.5}


def test_cell_value_matches_calculate_target_soc_directly() -> None:
    """Single source of truth: matrix cell == standalone `calculate_target_soc`."""
    pv = _PV_DEFICIT
    cons = _CONS_HIGH

    matrix = compute_matrix(
        pv_profiles_by_strategy={"only_pv": pv},
        cons_profiles_by_strategy={"only_cons": cons},
        cons_labels={},
        source_day_pv_sums={"only_cons": 2.0},
        start_charge_hour=None,
    )

    direct = calculate_target_soc(pv, consumption_profile=cons)

    assert matrix.cells_pct[("only_pv", "only_cons")] == direct.value


def test_start_charge_hour_propagated_to_cells() -> None:
    """Pre-charge gate clamps positive cumulative across the hour boundary."""
    # PV surplus only in hour 7 (pre-charge), deficit afterwards.
    pv = _pv_list([1.5, 1.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    cons = _cons(0.45)

    no_gate = compute_matrix(
        pv_profiles_by_strategy={"x": pv},
        cons_profiles_by_strategy={"y": cons},
        cons_labels={},
        source_day_pv_sums={"y": None},
        start_charge_hour=None,
    )
    with_gate = compute_matrix(
        pv_profiles_by_strategy={"x": pv},
        cons_profiles_by_strategy={"y": cons},
        cons_labels={},
        source_day_pv_sums={"y": None},
        start_charge_hour=8,
    )

    # Without the gate, the surplus from hour 7 offsets later deficit →
    # smaller required SOC%. With the gate, the surplus is dropped at
    # the 7→8 boundary → larger required SOC%.
    assert with_gate.cells_pct[("x", "y")] > no_gate.cells_pct[("x", "y")]


def test_dip_kwh_zero_when_no_deficit_negative_when_deficit() -> None:
    matrix = compute_matrix(
        pv_profiles_by_strategy={"generous": _PV_GENEROUS, "stingy": _PV_DEFICIT},
        cons_profiles_by_strategy={"cons": _CONS_BASE},
        cons_labels={},
        source_day_pv_sums={"cons": None},
        start_charge_hour=None,
    )
    assert matrix.cells_kwh[("generous", "cons")] == 0.0
    # Deficit: 0.15 - 0.45 = -0.3 kWh per bucket × 12 buckets = 3.6 kWh dip.
    assert abs(matrix.cells_kwh[("stingy", "cons")] - 3.6) < 0.01


def test_empty_inputs_return_empty_matrix() -> None:
    matrix = compute_matrix(
        pv_profiles_by_strategy={},
        cons_profiles_by_strategy={},
        cons_labels={},
        source_day_pv_sums={},
        start_charge_hour=None,
    )
    assert matrix.pv_strategies == ()
    assert matrix.cons_strategies == ()
    assert matrix.cells_pct == {}
    assert matrix.cells_kwh == {}
    assert matrix.pv_sums_kwh == {}
    assert matrix.cons_sums_kwh == {}


def test_now_aware_matrix_matches_sensor_semantics() -> None:
    """Passing `now` time-prorates in-progress bucket — cell == standalone sensor."""
    from datetime import datetime, timezone

    # PV deficit in early morning, surplus afterwards.
    pv = _pv_list([0.1, 0.1, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6])
    cons = _cons(0.45)
    now = datetime(2026, 4, 18, 8, 0, tzinfo=timezone.utc)

    full_window = compute_matrix(
        pv_profiles_by_strategy={"pv": pv},
        cons_profiles_by_strategy={"cons": cons},
        cons_labels={},
        source_day_pv_sums={"cons": None},
        start_charge_hour=None,
    )
    now_aware = compute_matrix(
        pv_profiles_by_strategy={"pv": pv},
        cons_profiles_by_strategy={"cons": cons},
        cons_labels={},
        source_day_pv_sums={"cons": None},
        start_charge_hour=None,
        now=now,
    )

    # Full-window catches the 7:00 + 7:30 deficit → higher SOC required.
    # Now-aware skips those past buckets → lower SOC.
    assert full_window.cells_pct[("pv", "cons")] > now_aware.cells_pct[("pv", "cons")]

    # Now-aware cell matches what `calculate_target_soc(..., now=now)` returns.
    direct = calculate_target_soc(pv, consumption_profile=cons, now=now)
    assert now_aware.cells_pct[("pv", "cons")] == direct.value


def test_cons_label_defaults_to_key_when_missing() -> None:
    matrix = compute_matrix(
        pv_profiles_by_strategy={"pv": _PV_GENEROUS},
        cons_profiles_by_strategy={"unlabeled": _CONS_BASE},
        cons_labels={},
        source_day_pv_sums={"unlabeled": None},
        start_charge_hour=None,
    )
    assert matrix.cons_strategies == (ConsLabel(key="unlabeled"),)
