"""TargetSocMatrix — full PV × Cons strategy comparison for the 7-13 window.

Crosses every (PV strategy, Cons baseline) pair, delegates each cell to
the same `calculate_target_soc` formula as the per-sensor variants
(single source of truth) and packages results plus row/column summaries.

Inputs are pre-computed 30-min bucket lists (kWh per bucket, 12 entries
for hours 7..12:30) so the matrix layer stays pure — application code
extracts them from `PvForecast` adjusted forecasts / ConsumptionProfile
loaders / RealizedPvLoader respectively.

Returns a `TargetSocMatrix` value object: dicts of cells keyed by
(pv_key, cons_key), plus row sums (per PV), column sums (per Cons), and
the actual realized PV sum on each prev-workday (None for Cons:Live —
that comparison cell makes no sense). Application code projects this
into the dashboard markdown cards or the service response.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from .pv_forecast import AdjustedPeriod, AdjustedPvForecast, ConsumptionProfile
from .target_soc import TargetSocResult, calculate_target_soc

# 7:00..12:30 in 30-min steps = 12 buckets. Matrix never extends beyond
# the deficit window; using a fixed length keeps the contract explicit.
_BUCKETS_PER_WINDOW = 12
_WINDOW_START_HOUR = 7
# Reference date used to anchor synthesized `AdjustedPeriod.period_start`
# values when calling `calculate_target_soc`. The formula only reads the
# hour/minute of each period, never the absolute date, so the choice is
# arbitrary — pin to a fixed date for determinism in tests.
_REFERENCE_DATE = date(2026, 1, 1)


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
    pv_buckets_by_strategy: dict[str, list[float]],
    cons_buckets_by_strategy: dict[str, list[float]],
    cons_labels: dict[str, ConsLabel],
    source_day_pv_sums: dict[str, float | None],
    start_charge_hour: int | None,
) -> TargetSocMatrix:
    """Cross every (PV, Cons) pair, compute target SOC% + dip kWh per cell.

    Reuses `calculate_target_soc` so the formula + `start_charge_hour`
    clamp are identical to the per-sensor variants. Bucket lists are
    expected to be length 12 (7:00..12:30); any other length is treated
    as missing data and produces no cells for that strategy.

    `source_day_pv_sums` holds the actual realized PV (kWh, 7-13) on
    each Prev-day source, projected as a bottom row in the dashboard.
    Set to None for the Live cons strategy where the concept doesn't
    apply.
    """
    pv_keys: tuple[str, ...] = tuple(pv_buckets_by_strategy.keys())
    cons_keys_ordered = list(cons_buckets_by_strategy.keys())
    cons_strategies: tuple[ConsLabel, ...] = tuple(
        cons_labels.get(k, ConsLabel(key=k)) for k in cons_keys_ordered
    )

    cells_pct: dict[tuple[str, str], int] = {}
    cells_kwh: dict[tuple[str, str], float] = {}
    pv_sums_kwh: dict[str, float] = {}
    cons_sums_kwh: dict[str, float] = {}

    for pv_key in pv_keys:
        pv_buckets = pv_buckets_by_strategy[pv_key]
        if len(pv_buckets) != _BUCKETS_PER_WINDOW:
            continue
        pv_sums_kwh[pv_key] = round(sum(pv_buckets), 3)
        forecast = _synthesize_forecast(pv_buckets)
        for cons_key in cons_keys_ordered:
            cons_buckets = cons_buckets_by_strategy[cons_key]
            if len(cons_buckets) != _BUCKETS_PER_WINDOW:
                continue
            profile = _synthesize_profile(cons_buckets)
            result = calculate_target_soc(
                forecast,
                consumption_profile=profile,
                start_charge_hour=start_charge_hour,
            )
            cells_pct[(pv_key, cons_key)] = result.value
            cells_kwh[(pv_key, cons_key)] = _dip_kwh(result)

    for cons_key in cons_keys_ordered:
        cons_buckets = cons_buckets_by_strategy[cons_key]
        if len(cons_buckets) == _BUCKETS_PER_WINDOW:
            cons_sums_kwh[cons_key] = round(sum(cons_buckets), 3)

    return TargetSocMatrix(
        pv_strategies=pv_keys,
        cons_strategies=cons_strategies,
        cells_pct=cells_pct,
        cells_kwh=cells_kwh,
        pv_sums_kwh=pv_sums_kwh,
        cons_sums_kwh=cons_sums_kwh,
        source_day_pv_sums_kwh=dict(source_day_pv_sums),
    )


# --- helpers ---


def _synthesize_forecast(pv_buckets: list[float]) -> AdjustedPvForecast:
    """Wrap 12 kWh-per-30min values as an `AdjustedPvForecast`.

    `calculate_target_soc` consumes `pv_estimate_adjusted` as an hourly
    rate and divides by 2 internally, so we multiply by 2 here to keep
    the formula contract intact.
    """
    periods = [
        AdjustedPeriod(
            period_start=_iso_period(idx),
            pv_estimate_adjusted=round(pv_buckets[idx] * 2, 4),
        )
        for idx in range(_BUCKETS_PER_WINDOW)
    ]
    total_kwh = round(sum(pv_buckets), 4)
    return AdjustedPvForecast(forecast=periods, total_kwh=total_kwh)


def _synthesize_profile(cons_buckets: list[float]) -> ConsumptionProfile:
    """Wrap 12 kWh-per-30min values as a `ConsumptionProfile`."""
    buckets: dict[tuple[int, int], float] = {}
    for idx in range(_BUCKETS_PER_WINDOW):
        hour, minute = _hour_minute(idx)
        buckets[(hour, minute)] = cons_buckets[idx]
    return ConsumptionProfile(buckets=buckets, source_date=None)


def _hour_minute(idx: int) -> tuple[int, int]:
    """Map bucket index 0..11 → (hour, minute) within the 7-13 window."""
    hour = _WINDOW_START_HOUR + idx // 2
    minute = (idx % 2) * 30
    return hour, minute


def _iso_period(idx: int) -> str:
    hour, minute = _hour_minute(idx)
    return datetime.combine(_REFERENCE_DATE, time(hour, minute, 0)).isoformat()


def _dip_kwh(result: TargetSocResult) -> float:
    """Most negative cumulative balance from the trace → absolute kWh dip.

    `0.0` when the bucket never went negative (PV covered consumption).
    """
    if not result.buckets:
        return 0.0
    min_cum = min(b.cumulative for b in result.buckets)
    return round(abs(min_cum), 3) if min_cum < 0 else 0.0
