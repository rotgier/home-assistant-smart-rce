"""TargetSocMatrix — full PV × Cons strategy comparison for the 7-13 window.

Crosses every (PV strategy, Cons baseline) pair, delegates each cell to
the same `calculate_target_soc` formula as the per-sensor variants
(single source of truth) and packages results plus row/column summaries.

Inputs are strict-contract `PvProfile` + `ConsumptionProfile` value
objects (12 buckets each, 7:00..12:30). Application code builds them
from `TargetSocCatalog.adjusted_*` via `to_profile(target_date)`, from
`ConsumptionProfileLoader` results, or from synthetic baselines via
`ConsumptionProfile.flat()`.

Returns a `TargetSocMatrix` value object: dicts of cells keyed by
(pv_key, cons_key), plus row sums (per PV), column sums (per Cons), and
the actual realized PV sum on each prev-workday (None for Cons:Live —
that comparison cell makes no sense). Application code projects this
into the dashboard markdown cards or the service response.
"""

from __future__ import annotations

from dataclasses import dataclass

from .consumption_profiles import ConsumptionProfile
from .target_soc import PvProfile, TargetSocResult, calculate_target_soc


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


def compute_matrix(
    pv_profiles_by_strategy: dict[str, PvProfile],
    cons_profiles_by_strategy: dict[str, ConsumptionProfile],
    cons_labels: dict[str, ConsLabel],
    source_day_pv_sums: dict[str, float | None],
    start_charge_hour: int | None,
) -> TargetSocMatrix:
    """Cross every (PV, Cons) pair, compute target SOC% + dip kWh per cell.

    Reuses `calculate_target_soc` so the formula + `start_charge_hour`
    clamp are identical to the per-sensor variants.

    Time-awareness lives on the input profiles — for today's matrix the
    caller builds profiles via `PvForecastResult.to_profile(target_date,
    now, pv_power_w_5min)` + `ConsumptionProfile.to_view(now,
    live_consumption_w)`, baking the in-progress bucket override into the
    bucket values. Matrix cells then match the per-strategy bridging
    sensors (matrix `Live × Live` == `sensor.rce_target_battery_soc_live`).
    For tomorrow / non-today: caller passes plain forecast / historical
    profiles (no `now`).

    `source_day_pv_sums` holds the actual realized PV (kWh, 7-13) on
    each Prev-day source, projected as a bottom row in the dashboard.
    Set to None for the Live cons strategy where the concept doesn't
    apply.
    """
    pv_keys: tuple[str, ...] = tuple(pv_profiles_by_strategy.keys())
    cons_keys_ordered = list(cons_profiles_by_strategy.keys())
    cons_strategies: tuple[ConsLabel, ...] = tuple(
        cons_labels.get(k, ConsLabel(key=k)) for k in cons_keys_ordered
    )

    cells_pct: dict[tuple[str, str], int] = {}
    cells_kwh: dict[tuple[str, str], float] = {}
    pv_sums_kwh: dict[str, float] = {}
    cons_sums_kwh: dict[str, float] = {}

    for pv_key, pv_profile in pv_profiles_by_strategy.items():
        pv_sums_kwh[pv_key] = round(sum(pv_profile.buckets.values()), 3)
        for cons_key in cons_keys_ordered:
            cons_profile = cons_profiles_by_strategy[cons_key]
            result = calculate_target_soc(
                pv_profile,
                consumption_profile=cons_profile,
                start_charge_hour=start_charge_hour,
            )
            cells_pct[(pv_key, cons_key)] = result.value
            cells_kwh[(pv_key, cons_key)] = _dip_kwh(result)

    for cons_key, cons_profile in cons_profiles_by_strategy.items():
        cons_sums_kwh[cons_key] = round(sum(cons_profile.buckets.values()), 3)

    return TargetSocMatrix(
        pv_strategies=pv_keys,
        cons_strategies=cons_strategies,
        cells_pct=cells_pct,
        cells_kwh=cells_kwh,
        pv_sums_kwh=pv_sums_kwh,
        cons_sums_kwh=cons_sums_kwh,
        source_day_pv_sums_kwh=dict(source_day_pv_sums),
    )


def _dip_kwh(result: TargetSocResult) -> float:
    """Most negative cumulative balance from the trace → absolute kWh dip.

    `0.0` when the bucket never went negative (PV covered consumption).
    """
    if not result.buckets:
        return 0.0
    min_cum = min(b.cumulative for b in result.buckets)
    return round(abs(min_cum), 3) if min_cum < 0 else 0.0
