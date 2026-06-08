"""TargetSocMatrix — VOs for the PV × Cons strategy matrix dashboard payload.

The per-cell computation lives on `TargetSocCatalog.target_socs[variant]`
(each `TargetSoc` persistuje `pv_profile` + `cons_view_flat` +
`cons_views_prev` after recalc, plus `flat.dip_kwh`/`prev_days[N].dip_kwh`
fields). `TargetSocMatrixService` reads those persisted values + serializes
into the matrix payload — see `application/target_soc_matrix_service.py`.

This module only defines the result VOs (`ConsLabel`, `TargetSocMatrix`).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConsLabel:
    """Display label for one Cons-strategy column.

    `key` is the stable identifier used in matrix cell tuples;
    `weekday` is the short English abbreviation (Mon/.../Fri) for Prev*
    columns, or None for the synthetic "Live" baseline.
    """

    key: str
    weekday: str | None = None


@dataclass(frozen=True)
class TargetSocMatrix:
    """All cells of a single matrix render plus row/column summaries."""

    pv_strategies: tuple[str, ...]
    cons_strategies: tuple[ConsLabel, ...]
    cells_pct: dict[tuple[str, str], int]
    cells_kwh: dict[tuple[str, str], float]
    pv_sums_kwh: dict[str, float]
    cons_sums_kwh: dict[str, float]
    source_day_pv_sums_kwh: dict[str, float | None]
