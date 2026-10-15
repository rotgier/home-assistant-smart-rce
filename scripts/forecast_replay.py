#!/usr/bin/env python3
"""Replay the forecast archive on production strategies and score the variants.

Reads `archive/forecast` (snapshots) and `archive/energy` (actuals) — a local
copy of `/config/smart_rce/archive` — and, per day:
1. replays every snapshot's context on fresh strategy instances
   (`domain.forecast_score.variant_results.replay` — the scoreboard's own code),
   plus variants production does not run (Live Soft with another alpha, raw
   Solcast);
2. picks the snapshot each decision moment consumed;
3. scores every variant at that moment, in several framings (each number is
   labelled with its framing — window sum, 30-min WAPE, decision variable).

Decision variables use production code and the ACTUAL consumption, so only the
PV forecast differs between the forecast and the actual side:
- morning-charge moments: the cumulative-deficit dip over 07:00–12:30 that
  drives the target SOC (`_calculate_target_soc`), in kWh and SOC points;
- discharge moments: PV surplus (PV − consumption without water heating) from
  the next slot to 16:00, capped to what the battery can take.
Sign convention: positive = forecast too OPTIMISTIC (more PV than came).

Usage:
    .venv/bin/python scripts/forecast_replay.py ARCHIVE_DIR FIRST_DAY LAST_DAY
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
import statistics
import sys
from typing import Final

# Run as a plain script: make the repo root (custom_components/) importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.smart_rce.domain.bucket import (  # noqa: E402
    PV_WINDOW_HOURS,
    Bucket,
    Buckets,
)
from custom_components.smart_rce.domain.consumption_profiles import (  # noqa: E402
    ConsumptionProfile,
)
from custom_components.smart_rce.domain.energy_day import EnergyDay  # noqa: E402
from custom_components.smart_rce.domain.forecast_score.variant_results import (  # noqa: E402
    ScoredSnapshot,
    replay,
)
from custom_components.smart_rce.domain.pv_forecast import (  # noqa: E402
    PvForecastResult,
)
from custom_components.smart_rce.domain.pv_forecast.forecast_snapshot import (  # noqa: E402
    ForecastSnapshot,
)
from custom_components.smart_rce.domain.target_soc import (  # noqa: E402
    BATTERY_CAPACITY_KWH,
    LOSS_FACTOR,
    MIN_SOC_PERCENT,
    PvProfile,
    _calculate_target_soc,
)
from custom_components.smart_rce.infrastructure.daily_jsonl_archive import (  # noqa: E402
    DailyJsonlArchive,
)

SLOT: Final = timedelta(minutes=30)
WINDOW_END: Final = time(16, 0)
# Usable battery above the minimum SOC: the most a surplus can refill.
SURPLUS_CAP_KWH: Final = BATTERY_CAPACITY_KWH * (1 - MIN_SOC_PERCENT / 100)
# Decision moments: (label, local time, kind). The snapshot consumed is the
# first one taken at or after that time (within TOLERANCE).
MOMENTS: Final = (
    ("04:00", time(4, 0), "charge"),
    ("06:10", time(6, 10), "charge"),
    ("06:40", time(6, 40), "charge"),
    ("07:40", time(7, 40), "discharge"),
    ("08:40", time(8, 40), "discharge"),
    ("09:40", time(9, 40), "discharge"),
)
TOLERANCE: Final = timedelta(minutes=15)


SHOWN: Final = (
    "solcast_p50",
    "at_6",
    "live",
    "live_soft_0.3",
    "live_soft",  # production: alpha 0.4 since 15.10.2026
    "live_soft_0.5",
    "live_soft_0.6",
    "extrapolated_live_proportional",
    "extrapolated_live_blend",
)


@dataclass(frozen=True)
class Score:
    """One variant at one decision moment of one day (energies in kWh)."""

    day: date
    moment: str
    variant: str
    forecast_kwh: float  # window sum
    actual_kwh: float
    abs_slot_err_kwh: float  # Σ|F−A| over 30-min slots
    decision_err_kwh: float  # + = optimistic
    decision_err_pp: float | None  # SOC points (charge moments only)


def main(root: Path, first: date, last: date) -> None:
    forecast = DailyJsonlArchive(root / "forecast")
    energy = DailyJsonlArchive(root / "energy")
    scores: list[Score] = []
    day = first
    while day <= last:
        snapshots = [ForecastSnapshot.from_dict(r) for r in forecast.read_day(day)]
        actual = [EnergyDay.from_dict(r) for r in energy.read_day(day)]
        if snapshots and actual:
            scores += score_day(day, replay(snapshots), actual[0])
        else:
            print(f"{day}: missing snapshots or actuals, skipped")
        day += timedelta(days=1)
    report(scores)


def score_day(
    day: date, replayed: list[ScoredSnapshot], energy: EnergyDay
) -> list[Score]:
    pv = slots(energy, "pv_kwh")
    cons = slots(energy, "consumption_minus_water_kwh")
    scores = []
    for label, at, kind in MOMENTS:
        moment = datetime.combine(day, at, energy.start.tzinfo)
        chosen = next(
            (r for r in replayed if moment <= r.taken_at <= moment + TOLERANCE),
            None,
        )
        if chosen is None:
            continue
        taken = chosen.taken_at
        first_slot = _next_slot(taken)
        window = [s for s in pv if first_slot <= s and s.time() < WINDOW_END]
        for name in SHOWN:
            result = chosen.results.get(name)
            if result is None:
                continue
            fc = forecast_slots(result, day)
            if not all(s in fc for s in window):
                continue
            err = (
                _dip_error(fc, pv, cons, day, chosen.context.start_charge_hour)
                if kind == "charge"
                else (_surplus(fc, cons, window) - _surplus(pv, cons, window), None)
            )
            scores.append(
                Score(
                    day=day,
                    moment=label,
                    variant=name,
                    forecast_kwh=sum(fc[s] for s in window),
                    actual_kwh=sum(pv[s] for s in window),
                    abs_slot_err_kwh=sum(abs(fc[s] - pv[s]) for s in window),
                    decision_err_kwh=err[0],
                    decision_err_pp=err[1],
                )
            )
    return scores


def slots(energy: EnergyDay, series: str) -> dict[datetime, float]:
    """5-min series → energy per 30-min slot, keyed by local slot start."""
    out: dict[datetime, float] = defaultdict(float)
    for index, value in enumerate(energy.series.get(series, [])):
        if value is None:
            continue
        start = (energy.start + EnergyDay.STEP * index).astimezone(energy.start.tzinfo)
        out[start.replace(minute=0 if start.minute < 30 else 30)] += value
    return dict(out)


def _next_slot(moment: datetime) -> datetime:
    """First 30-min slot that starts at or after `moment` (future only)."""
    base = moment.replace(
        minute=0 if moment.minute < 30 else 30, second=0, microsecond=0
    )
    return base if base == moment else base + SLOT


def forecast_slots(result: PvForecastResult, day: date) -> dict[datetime, float]:
    """Forecast energy per slot — the archive stores RATES (kWh/h): energy = rate/2."""
    out = {}
    for period in result.forecast:
        start = datetime.fromisoformat(period.period_start)
        if start.date() == day:
            out[start] = period.pv_estimate_adjusted / 2
    return out


def _dip_error(
    fc: dict[datetime, float],
    pv: dict[datetime, float],
    cons: dict[datetime, float],
    day: date,
    start_charge_hour: int | None,
) -> tuple[float, float]:
    """(actual dip − forecast dip) in kWh and in target-SOC points; + = optimistic."""
    consumption = _profile(cons, day)
    forecast_dip = _calculate_target_soc(
        PvProfile(buckets=Buckets(by_bucket=bucket_map(fc, day))),
        ConsumptionProfile(buckets=Buckets(by_bucket=consumption)),
        start_charge_hour,
    ).dip_kwh
    actual_dip = _calculate_target_soc(
        PvProfile(buckets=Buckets(by_bucket=bucket_map(pv, day))),
        ConsumptionProfile(buckets=Buckets(by_bucket=consumption)),
        start_charge_hour,
    ).dip_kwh
    err = actual_dip - forecast_dip
    return err, err / (BATTERY_CAPACITY_KWH / 100) * (1 + LOSS_FACTOR)


def _profile(slots: dict[datetime, float], day: date) -> dict[Bucket, float]:
    return bucket_map(slots, day)


def bucket_map(slots: dict[datetime, float], day: date) -> dict[Bucket, float]:
    tz = next(iter(slots)).tzinfo if slots else None
    return {
        Bucket(h, m): slots.get(datetime.combine(day, time(h, m), tz), 0.0)
        for h in PV_WINDOW_HOURS
        for m in (0, 30)
    }


def _surplus(
    pv: dict[datetime, float], cons: dict[datetime, float], window: list[datetime]
) -> float:
    """Net PV − consumption over the window, capped to what the battery can take."""
    net = sum(pv[s] - cons.get(s, 0.0) for s in window)
    return min(max(net, 0.0), SURPLUS_CAP_KWH)


# --- report ---------------------------------------------------------------- #


def report(scores: list[Score]) -> None:
    days = sorted({s.day for s in scores})
    print(f"\n{len(days)} days: {days[0]} … {days[-1]}")
    for label, _, kind in MOMENTS:
        rows = [s for s in scores if s.moment == label]
        if not rows:
            continue
        decision = (
            "dip T1 07–12:30 (SOC pp)" if kind == "charge" else "surplus →16:00 capped"
        )
        print(f"\n=== {label} — window: next slot → 16:00; decision: {decision} ===")
        print(
            f"{'variant':32}{'n':>3} {'sum bias':>9} {'|sum|':>7} {'WAPE30':>7}"
            f" {'dec MAE kWh':>11} {'dec bias':>9} {'opt>1kWh':>9}"
            + (f" {'MAE pp':>7}" if kind == "charge" else "")
        )
        for name in SHOWN:
            mine = [s for s in rows if s.variant == name]
            if not mine:
                continue
            actual = sum(s.actual_kwh for s in mine)
            bias = (sum(s.forecast_kwh for s in mine) - actual) / actual
            abs_sum = statistics.median(
                abs(s.forecast_kwh - s.actual_kwh) / s.actual_kwh
                for s in mine
                if s.actual_kwh > 0.5
            )
            wape = sum(s.abs_slot_err_kwh for s in mine) / actual
            errs = [s.decision_err_kwh for s in mine]
            line = (
                f"{name:32}{len(mine):>3} {bias:>+9.3f} {abs_sum:>7.3f} {wape:>7.3f}"
                f" {statistics.fmean(abs(e) for e in errs):>11.2f}"
                f" {statistics.fmean(errs):>+9.2f} {sum(e > 1 for e in errs):>9}"
            )
            if kind == "charge":
                pp = [abs(s.decision_err_pp or 0) for s in mine]
                line += f" {statistics.fmean(pp):>7.1f}"
            print(line)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(
        Path(sys.argv[1]),
        date.fromisoformat(sys.argv[2]),
        date.fromisoformat(sys.argv[3]),
    )
