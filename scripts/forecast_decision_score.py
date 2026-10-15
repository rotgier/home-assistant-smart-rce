#!/usr/bin/env python3
"""Score PV forecast variants and consumption models by the decisions they drive.

Builds on `forecast_replay.replay` (archived contexts through production
strategies) and uses production functions for both decisions:

D1 — morning reserve until 13:00 (`_calculate_target_soc`, T1 reserve rule
     max(flat, prev-workdays without the worst)), moments: evening 20:01 of the
     day before (tomorrow variants), 04:00, 06:10, 06:40. Workdays only.
     PV part of the error (variant ranking): R(PV_actual) − R(PV_forecast) with
     the same consumption rule; + = forecast optimistic = battery under-charged.
     Consumption part (same for every variant): f(PV_act, C_act) − R(PV_act).
D2 — charge start (`find_candidate`), moments 07:41 / 08:41 / 09:41, actual
     SOC at the moment, the day's RCE prices, S0 from `ChargeSlots.compute`.
     Benchmark S* = the same rule on actual PV with flat consumption (as
     production); + Δ = start too late (battery short), − = too early.

Usage:
    .venv/bin/python scripts/forecast_decision_score.py DATA_DIR FIRST LAST
DATA_DIR holds forecast/, energy/ (archive copies) and extra/rce_prices.json.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import statistics
import sys
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import forecast_replay as fr  # noqa: E402

from custom_components.smart_rce.domain.bucket import (  # noqa: E402
    PV_WINDOW_HOURS,
    Bucket,
    Buckets,
)
from custom_components.smart_rce.domain.charge_curve import kwh_to_full  # noqa: E402
from custom_components.smart_rce.domain.charge_slots import ChargeSlots  # noqa: E402
from custom_components.smart_rce.domain.charge_start_shift import (  # noqa: E402
    find_candidate,
    price_window_end,
)
from custom_components.smart_rce.domain.consumption_profiles import (  # noqa: E402
    ConsumptionProfile,
)
from custom_components.smart_rce.domain.energy_day import EnergyDay  # noqa: E402
from custom_components.smart_rce.domain.pv_forecast import (  # noqa: E402
    AdjustedPeriod,
    PvForecastResult,
)
from custom_components.smart_rce.domain.pv_forecast.forecast_snapshot import (  # noqa: E402
    ForecastSnapshot,
)
from custom_components.smart_rce.domain.pv_surplus import SurplusProfile  # noqa: E402
from custom_components.smart_rce.domain.rce import RceDayPrices  # noqa: E402
from custom_components.smart_rce.domain.target_soc import (  # noqa: E402
    BATTERY_CAPACITY_KWH,
    CONSUMPTION_PER_30MIN,
    LOSS_FACTOR,
    MIN_SOC_PERCENT,
    PvProfile,
    _calculate_target_soc,
)
from custom_components.smart_rce.infrastructure.daily_jsonl_archive import (  # noqa: E402
    DailyJsonlArchive,
)

PREV_WORKDAYS: Final = 8
D1_MOMENTS: Final = (
    ("20:01 dzień przed", None),
    ("04:01", time(4, 1)),
    ("06:11", time(6, 11)),
    ("06:41", time(6, 41)),
)
D2_MOMENTS: Final = (
    ("07:41", time(7, 41)),
    ("08:41", time(8, 41)),
    ("09:16", time(9, 16)),
    ("09:41", time(9, 41)),
)
D1_VARIANTS: Final = (
    "solcast_p50",
    "at_6",
    "live",
    "live_soft_0.3",
    "live_soft",  # production: alpha 0.4 since 15.10.2026
    "live_soft_0.5",
    "live_soft_0.6",
)
D1_EVENING: Final = {
    "solcast_p50": "tomorrow_p50",
    "live": "tomorrow_live",
    "live_soft": "tomorrow_live_soft",
    "at_6": "tomorrow_at_6",
}
D2_VARIANTS: Final = (
    "solcast_p50",
    "live",
    "live_soft_0.3",
    "live_soft",  # production: alpha 0.4 since 15.10.2026
    "live_soft_0.5",
    "live_soft_0.6",
    "extrapolated_live_proportional",
    "extrapolated_live_blend",
)


def main(root: Path, first: date, last: date) -> None:
    forecast = DailyJsonlArchive(root / "forecast")
    energy_archive = DailyJsonlArchive(root / "energy")
    prices = json.loads((root / "extra" / "rce_prices.json").read_text())
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    energy = {
        d: EnergyDay.from_dict(rows[0])
        for d in [first - timedelta(days=14 + i) for i in range(0)]
        + _all_days(energy_archive, first, last)
        if (rows := energy_archive.read_day(d))
    }
    replayed = {}
    for d in [first - timedelta(days=1), *days]:
        snaps = [ForecastSnapshot.from_dict(r) for r in forecast.read_day(d)]
        if snaps:
            replayed[d] = fr.replay(snaps)
    d1, cons_rows, prod_rows = [], [], []
    d2 = []
    for d in days:
        if d not in energy or d not in replayed:
            continue
        if d.weekday() < 5:
            r = score_d1(d, replayed, energy)
            if r:
                d1 += r[0]
                cons_rows += r[1]
                prod_rows.append(r[2])
        if d.isoformat() in prices:
            d2 += score_d2(d, replayed[d], energy, prices[d.isoformat()])
    report(d1, cons_rows, prod_rows, d2, days)


def _all_days(archive: DailyJsonlArchive, first: date, last: date) -> list[date]:
    out, d = [], first - timedelta(days=20)
    while d <= last:
        if archive.has_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def consumed(replayed, decision: datetime, max_age: timedelta = timedelta(minutes=40)):
    """Return the snapshot a decision at `decision` used: the latest before it."""
    before = [
        r for r in replayed if decision - max_age <= r.snapshot.taken_at <= decision
    ]
    return before[-1] if before else None


# --- D1: morning reserve ---------------------------------------------------- #

FLAT: Final = {
    Bucket(h, m): CONSUMPTION_PER_30MIN for h in PV_WINDOW_HOURS for m in (0, 30)
}


def _target(
    pv: dict[Bucket, float], cons: dict[Bucket, float], sch: int | None
) -> tuple[int, float]:
    r = _calculate_target_soc(
        PvProfile(buckets=Buckets(by_bucket=pv)),
        ConsumptionProfile(buckets=Buckets(by_bucket=cons)),
        sch,
    )
    return r.value, r.dip_kwh


def _rule(
    pv: dict[Bucket, float], prev: list[dict[Bucket, float]], sch: int | None
) -> int:
    """Production T1 reserve rule since 15.10.2026 (per-slot median).

    Median of the prev workdays per slot; flat when there are none.
    """
    if not prev:
        return _target(pv, FLAT, sch)[0]
    return _target(pv, _median_profile(prev), sch)[0]


def _old_rule(
    pv: dict[Bucket, float], prev: list[dict[Bucket, float]], sch: int | None
) -> int:
    """T1 reserve rule until 14.10.2026: max(flat, prev workdays without the worst)."""
    flat = _target(pv, FLAT, sch)[0]
    values = sorted((_target(pv, c, sch)[0] for c in prev), reverse=True)
    prev_ww = values[1] if len(values) > 1 else (values[0] if values else None)
    return max(flat, prev_ww) if prev_ww is not None else flat


def _prev_profiles(d: date, energy: dict[date, EnergyDay]) -> list[dict[Bucket, float]]:
    out, cand = [], d - timedelta(days=1)
    while len(out) < PREV_WORKDAYS and (d - cand).days <= 20:
        if cand.weekday() < 5 and cand in energy:
            out.append(
                fr.bucket_map(
                    fr.slots(energy[cand], "consumption_minus_water_kwh"), cand
                )
            )
        cand -= timedelta(days=1)
    return out


def _pp(kwh: float) -> float:
    return kwh / (BATTERY_CAPACITY_KWH / 100) * (1 + LOSS_FACTOR)


def score_d1(d, replayed, energy):
    en = energy[d]
    pv_act = fr.bucket_map(fr.slots(en, "pv_kwh"), d)
    cons_act = fr.bucket_map(fr.slots(en, "consumption_minus_water_kwh"), d)
    prev = _prev_profiles(d, energy)
    rows, cons_rows = [], []
    sch_any = None
    for label, at in D1_MOMENTS:
        if at is None:  # evening fetch of the day before
            src = [
                r
                for r in replayed.get(d - timedelta(days=1), [])
                if time(20, 0) <= r.snapshot.taken_at.time() <= time(20, 15)
            ]
            snap = src[0] if src else None
            names = D1_EVENING
        else:
            snap = consumed(replayed[d], datetime.combine(d, at, en.start.tzinfo))
            names = {v: v for v in D1_VARIANTS}
        if snap is None:
            continue
        sch = snap.snapshot.context.start_charge_hour if at is not None else None
        sch_any = sch if at == time(4, 1) else sch_any
        r_act = _rule(pv_act, prev, sch)
        for name, key in names.items():
            res = (
                snap.results.get(key)
                if key != "tomorrow_p50"
                else _p50(snap.snapshot.context.solcast_tomorrow)
            )
            if res is None:
                continue
            fc = fr.forecast_slots(res, d)
            if not fc:
                continue
            pv_fc = fr.bucket_map(fc, d)
            rows.append((d, label, name, r_act - _rule(pv_fc, prev, sch)))
    # consumption models, judged with ACTUAL PV against actual consumption
    sch = sch_any
    truth_value, truth_dip = _target(pv_act, cons_act, sch)
    models = {
        "flat 0,9 kWh/h": [FLAT],
        "poprz. dni: mediana (prod od 15.10)": [_median_profile(prev)] if prev else [],
        "poprz. dni: bez najgorszego": None,
        "prod do 14.10: max(flat, bez najg.)": "old_rule",
    }
    for name, profiles in models.items():
        if profiles == "old_rule":
            value = _old_rule(pv_act, prev, sch)
        elif profiles is None:
            vals = sorted((_target(pv_act, c, sch)[0] for c in prev), reverse=True)
            if not vals:
                continue
            value = vals[1] if len(vals) > 1 else vals[0]
        elif profiles:
            value = _target(pv_act, profiles[0], sch)[0]
        else:
            continue
        cons_rows.append((d, name, truth_value - value, len(prev)))
    need_raw = MIN_SOC_PERCENT + _pp(truth_dip)
    soc = [
        v
        for i, v in enumerate(en.series["battery_soc_pct"])
        if v is not None and 84 <= i < 156
    ]  # 07:00-13:00
    grid = [
        v
        for i, v in enumerate(en.series["grid_net_export_kwh"])
        if v is not None and 84 <= i < 156
    ]
    prod = (
        d,
        truth_value,
        round(need_raw, 1),
        min(soc) if soc else None,
        round(-sum(min(g, 0) for g in grid), 2),
        len(prev),
    )
    return rows, cons_rows, prod


def _p50(periods) -> PvForecastResult | None:
    if not periods:
        return None
    return PvForecastResult(
        [AdjustedPeriod(p.period_start, p.pv_estimate) for p in periods], 0.0
    )


def _median_profile(profiles: list[dict[Bucket, float]]) -> dict[Bucket, float]:
    return {b: statistics.median(p[b] for p in profiles) for b in profiles[0]}


# --- D2: charge start -------------------------------------------------------- #


def score_d2(d, replayed, energy, prices):
    en = energy[d]
    tz = en.start.tzinfo
    window = ChargeSlots.compute(RceDayPrices(None, d, tuple(prices)))
    if window is None:
        return []
    s0 = int(window.start_hour * 60)
    pv = fr.slots(en, "pv_kwh")
    cons = fr.slots(en, "consumption_minus_water_kwh")
    actual_flat = SurplusProfile.build(d, _by_index(pv), None)
    actual_real = SurplusProfile.build(d, _by_index(pv), _by_index(cons))
    prev_cons = [
        _by_index(fr.slots(energy[p], "consumption_minus_water_kwh"))
        for p in _prev_days(d, energy)
    ]
    median_cons = (
        {k: statistics.median(c.get(k, 0.45) for c in prev_cons) for k in range(48)}
        if prev_cons
        else None
    )
    out = []
    for label, at in D2_MOMENTS:
        snap = consumed(replayed, datetime.combine(d, at, tz))
        if snap is None:
            continue
        now = at.hour * 60 + at.minute
        if now >= s0:
            continue  # production evaluates only before S0
        idx = (at.hour * 60 + at.minute) // 5
        soc = next(
            (
                en.series["battery_soc_pct"][i]
                for i in range(idx, idx - 6, -1)
                if en.series["battery_soc_pct"][i] is not None
            ),
            None,
        )
        if soc is None:
            continue
        need = kwh_to_full(soc)
        star = find_candidate(actual_flat, prices, now, need, s0)
        star_real = find_candidate(actual_real, prices, now, need, s0)
        for name in D2_VARIANTS:
            res = snap.results.get(name)
            if res is None:
                continue
            fc = fr.forecast_slots(res, d)
            prof = SurplusProfile.build(d, _by_index(fc), None)
            cand = find_candidate(prof, prices, now, need, s0)
            out.append(_classify(d, label, name, cand, star, actual_flat, prices, need))
        for cname, cprof in (
            ("cons: flat", None),
            ("cons: mediana poprz. dni", median_cons),
        ):
            if cname != "cons: flat" and cprof is None:
                continue
            cand = find_candidate(
                SurplusProfile.build(d, _by_index(pv), cprof), prices, now, need, s0
            )
            out.append(
                _classify(d, label, cname, cand, star_real, actual_real, prices, need)
            )
    return out


def _prev_days(d, energy):
    out, c = [], d - timedelta(days=1)
    while len(out) < 5 and (d - c).days <= 20:
        if c in energy and (c.weekday() < 5) == (d.weekday() < 5):
            out.append(c)
        c -= timedelta(days=1)
    return out


def _by_index(slots: dict[datetime, float]) -> dict[int, float]:
    return {s.hour * 2 + (1 if s.minute >= 30 else 0): v for s, v in slots.items()}


def _classify(d, label, name, cand, star, actual, prices, need):  # noqa: PLR0917
    delta_h = (cand.start - star.start) / 60
    if not cand.enough and not star.enough:
        cls = "słusznie nie starczy"
    elif not cand.enough and star.enough:
        cls = "niesłusznie nie starczy"
    elif abs(delta_h) < 0.5:
        cls = "na czas"
    elif delta_h > 0:
        cls = "za późno"
    else:
        cls = "za wcześnie"
    end = price_window_end(cand.start, prices) * 60
    shortfall = (
        max(0.0, need - actual.total(cand.start, end)) if cls == "za późno" else 0.0
    )
    early = actual.total(cand.start, star.start) if delta_h < 0 else 0.0
    return (d, label, name, cls, delta_h, shortfall, early)


# --- report ------------------------------------------------------------------ #


def report(d1, cons_rows, prod_rows, d2, days):
    print(f"Dni: {days[0]} … {days[-1]}")
    print(
        "\n## D1 — rezerwa do 13:00, część PV błędu [pp]: + = prognoza za optymistyczna (bateria za słabo naładowana)"
    )
    for label, _ in D1_MOMENTS:
        rows = [r for r in d1 if r[1] == label]
        if not rows:
            continue
        print(f"\n{label}:  wariant  n  MAE  bias  dni>+3pp  dni<-3pp   [per dzień]")
        for name in dict.fromkeys(r[2] for r in rows):
            e = [r[3] for r in rows if r[2] == name]
            print(
                f"  {name:16}{len(e):>3} {statistics.fmean(map(abs, e)):5.1f} {statistics.fmean(e):+5.1f} {sum(x > 3 for x in e):>6} {sum(x < -3 for x in e):>8}   {' '.join(f'{x:+d}' for x in e)}"
            )
    print(
        "\n## D1 — modele zużycia (z rzeczywistym PV) vs rzeczywiste zużycie bez CWU [pp]: + = model za niski (za mało rezerwy)"
    )
    for name in dict.fromkeys(r[1] for r in cons_rows):
        e = [r[2] for r in cons_rows if r[1] == name]
        print(
            f"  {name:30}{len(e):>3} MAE {statistics.fmean(map(abs, e)):5.1f} bias {statistics.fmean(e):+5.1f}   {' '.join(f'{x:+d}' for x in e)}"
        )
    print(
        "  (poprz. dni dostępnych na dzień: "
        + ", ".join(
            f"{r[0]:%d.%m}:{r[3]}" for r in cons_rows if r[1] == "flat 0,9 kWh/h"
        )
        + ")"
    )
    print("\n## D1 — co się naprawdę stało (rzeczywiste PV i zużycie)")
    print(
        "  dzień    potrzebne (z buforem) / bez bufora   min SOC 07-13   import T1 kWh"
    )
    for d, value, raw, min_soc, imp, _n in prod_rows:
        print(
            f"  {d:%a %d.%m}   {value:>4}% / {raw:>5}%            {min_soc if min_soc is None else round(min_soc):>4}%        {imp:>5}"
        )
    print("\n## D2 — start ładowania vs benchmark (ta sama reguła na rzeczywistym PV)")
    for label, _ in D2_MOMENTS:
        rows = [r for r in d2 if r[1] == label]
        if not rows:
            continue
        print(f"\n{label}:")
        for name in dict.fromkeys(r[2] for r in rows):
            mine = [r for r in rows if r[2] == name]
            counts = defaultdict(int)
            for r in mine:
                counts[r[3]] += 1
            short = sum(r[5] for r in mine)
            early = sum(r[6] for r in mine)
            print(
                f"  {name:32}{len(mine):>3}  "
                + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
                + f" | Σ|Δ| {sum(abs(r[4]) for r in mine):.1f} h, niedobór {short:.1f} kWh, za wcześnie {early:.1f} kWh"
            )


if __name__ == "__main__":
    main(
        Path(sys.argv[1]),
        date.fromisoformat(sys.argv[2]),
        date.fromisoformat(sys.argv[3]),
    )
