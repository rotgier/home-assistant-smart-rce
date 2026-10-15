#!/usr/bin/env python3
"""Score PV forecast variants and consumption models by the decisions they drive.

A thin offline wrapper over `domain.forecast_score` — the code Home Assistant
runs every evening — on a local copy of the archive. It differs from the HA
run only in its inputs:
- variants are REPLAYED on fresh strategy instances (HA reads production's
  archived results, adding only what the archive lacks) — a variant new in
  code gets scored on the whole archive;
- earlier days of consumption come from the archive alone (workdays by
  `DayType`, not the workday calendar; no hourly fallback before 03.10);
- the start that struck comes from the SoC rise (no entity history offline),
  and the afternoon top-up is unknown.
Model of the score: home-assistant-ops `plans/forecast-score-framework.md`.

Usage:
    .venv/bin/python scripts/forecast_decision_score.py DATA_DIR FIRST LAST [OUT.json]
DATA_DIR holds forecast/, energy/ (archive copies) and extra/rce_prices.json.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
from typing import Any, Final
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.smart_rce.domain.charge_slots import ChargeSlots  # noqa: E402
from custom_components.smart_rce.domain.energy_day import EnergyDay  # noqa: E402
from custom_components.smart_rce.domain.forecast_score.day_actuals import (  # noqa: E402
    DayActuals,
)
from custom_components.smart_rce.domain.forecast_score.day_score import (  # noqa: E402
    DayInputs,
    DayScore,
    ScoreStatus,
    score_day,
)
from custom_components.smart_rce.domain.forecast_score.production_outcome import (  # noqa: E402
    ChargeStartFact,
    start_from_soc_rise,
)
from custom_components.smart_rce.domain.forecast_score.ranking import rank  # noqa: E402
from custom_components.smart_rce.domain.forecast_score.variant_results import (  # noqa: E402
    replay,
)
from custom_components.smart_rce.domain.pv_forecast.forecast_snapshot import (  # noqa: E402
    ForecastSnapshot,
)
from custom_components.smart_rce.domain.pv_surplus import DayType  # noqa: E402
from custom_components.smart_rce.domain.rce import RceDayPrices  # noqa: E402
from custom_components.smart_rce.infrastructure.daily_jsonl_archive import (  # noqa: E402
    DailyJsonlArchive,
)

TZ: Final = ZoneInfo("Europe/Warsaw")
CONSUMPTION: Final = "consumption_minus_water_kwh"
EARLIER_WORKDAYS: Final = 8
# How far back earlier days are looked for (the archive starts 03.10.2026).
LOOKBACK_DAYS: Final = 40


def main(root: Path, first: date, last: date, out: Path | None) -> None:
    forecast = DailyJsonlArchive(root / "forecast")
    energy = _energy(DailyJsonlArchive(root / "energy"), first, last)
    prices: dict[str, list[float]] = json.loads(
        (root / "extra" / "rce_prices.json").read_text()
    )
    scores: list[DayScore] = []
    day = first
    while day <= last:
        snapshots = [ForecastSnapshot.from_dict(r) for r in forecast.read_day(day)]
        if snapshots and day in energy:
            scores.append(_score(day, snapshots, energy, prices.get(day.isoformat())))
        else:
            print(f"{day}: missing snapshots or actuals, skipped")
        day += timedelta(days=1)
    report(scores)
    if out is not None:
        out.write_text(json.dumps([s.to_dict() for s in scores], ensure_ascii=False))


def _energy(
    archive: DailyJsonlArchive, first: date, last: date
) -> dict[date, EnergyDay]:
    found: dict[date, EnergyDay] = {}
    day = first - timedelta(days=LOOKBACK_DAYS)
    while day <= last:
        energy = EnergyDay.from_records(archive.read_day(day))
        if energy is not None:
            found[day] = energy
        day += timedelta(days=1)
    return found


def _score(
    day: date,
    snapshots: list[ForecastSnapshot],
    energy: dict[date, EnergyDay],
    prices: list[float] | None,
) -> DayScore:
    actuals = DayActuals(energy[day], TZ)
    window = (
        ChargeSlots.compute(RceDayPrices(None, day, tuple(prices))) if prices else None
    )
    s0 = round(window.start_hour * 60) if window is not None else None
    inputs = DayInputs(
        actuals=actuals,
        snapshots=replay(snapshots),
        earlier_workdays=_earlier_workdays(day, energy),
        earlier_same_type=_half_hours(DayType.of(day).previous_days(day), energy),
        prices=prices,
        s0=s0,
        start=_start(actuals, s0),
        afternoon_charge=None,
    )
    return score_day(
        inputs,
        status=ScoreStatus.FINAL,
        computed_at=datetime.now(TZ),
        code_version="offline",
    )


def _earlier_workdays(day: date, energy: dict[date, EnergyDay]) -> dict[date, Any]:
    days, back = [], day - timedelta(days=1)
    while len(days) < EARLIER_WORKDAYS and (day - back).days <= LOOKBACK_DAYS:
        if DayType.of(back) is DayType.WORKDAY:
            days.append(back)
        back -= timedelta(days=1)
    return _half_hours(days, energy)


def _half_hours(days: list[date], energy: dict[date, EnergyDay]) -> dict[date, Any]:
    return {
        d: energy[d].energy_by_half_hour(CONSUMPTION, TZ) for d in days if d in energy
    }


def _start(actuals: DayActuals, s0: int | None) -> ChargeStartFact:
    rise = start_from_soc_rise(actuals)
    return ChargeStartFact(rise, "soc_rise" if rise is not None else "none", s0)


# --- report ------------------------------------------------------------------ #


def report(scores: list[DayScore]) -> None:
    if not scores:
        return
    print(
        f"Dni: {scores[0].day} … {scores[-1].day}  (ujęcie: decyzje; pp = punkty SOC)"
    )
    print(
        "\n## Karta dnia: błąd systemu (system − rzeczywiście) = część PV + część zużycia; − = system za mało"
    )
    print(
        "  dzień       typ (Live/p50 06:11, rzecz./p50)   rezerwa 06:11 [pp]"
        "        linia 08:41   start [h]   import T1   min SOC   eksport 07-10"
    )
    for s in scores:
        kind = s.day_type.kind.value if s.day_type.kind else "–"
        split = (
            f"{s.reserve_error_pp:+d} = {s.reserve_error_pv_pp:+d} PV"
            f" {s.reserve_error_consumption_pp:+d} zuż."
            if s.reserve_error_pp is not None
            and s.reserve_error_pv_pp is not None
            and s.reserve_error_consumption_pp is not None
            else "–"
        )
        o = s.outcome
        print(
            f"  {s.day:%a %d.%m}  {kind:6} ({s.day_type.live_over_p50}, "
            f"{s.day_type.actual_pv_over_p50})  {split:24} "
            f"{_signed(s.reserve_line_error_pp):>8}   {_signed(s.start_delta_h):>8}"
            f"   {o.t1_import_kwh}   {o.min_soc_t1}   {o.morning_export_kwh}"
        )
    ranking = rank(scores)
    print(
        "\n## D1 — rezerwa, część PV [pp]: system − rzeczywiście; − = za mało rezerwy (groźne)"
    )
    for label, rows in ranking["reserve"]["all"].items():
        print(
            f"\n{label}:  wariant                             n   MAE  bias <-3 <-5 asym"
            " │ rzecz. zuż.: MAE bias │ vs p50: ΔMAE  W/R/P"
        )
        for r in rows:
            print(
                f"  {r['variant']:38}{r['n']:>2} {r['mae']:5.1f} {r['bias']:+5.1f}"
                f" {r['short_3']:>2} {r['short_5']:>2} {r['asym_loss']:4.1f} │"
                f" {r['mae_actual_cons']:5.1f} {r['bias_actual_cons']:+5.1f} │"
                f" {_signed(r['vs_p50_mae']):>6}  {r['wins']}/{r['ties']}/{r['losses']}"
            )
    print("\n## D1 — modele zużycia o 06:11 (rzeczywiste PV) [pp]: − = model za niski")
    for r in ranking["reserve_models"]["all"]:
        print(
            f"  {r['model']:10}{r['n']:>3}  MAE {r['mae']:5.1f}  bias {r['bias']:+5.1f}"
            f"  <-3 {r['short_3']}  │ z prognozą systemu: MAE {r['mae_production_pv']}"
            f" bias {r['bias_production_pv']}"
        )
    print("\n## D2 — start ładowania (system − wzorzec; + = za późno, groźne)")
    for label, rows in ranking["start"]["all"].items():
        print(f"\n{label}:")
        for r in rows:
            verdicts = ", ".join(f"{k} {v}" for k, v in sorted(r["verdicts"].items()))
            print(
                f"  {r['variant']:38}{r['n']:>3}  {verdicts} | MAE {r['mae_h']} h,"
                f" niedobór {r['short_kwh']} kWh, za wcześnie {r['early_kwh']} kWh"
                f" | vs p50 W/R/P {r['wins']}/{r['ties']}/{r['losses']}"
            )
    print("\n## D2 — modele zużycia (rzeczywiste PV vs wzorzec rzeczywisty)")
    for label, rows in ranking["start_models"]["all"].items():
        print(f"\n{label}:")
        for r in rows:
            verdicts = ", ".join(f"{k} {v}" for k, v in sorted(r["verdicts"].items()))
            print(
                f"  {r['model']:10}{r['n']:>3}  {verdicts} | niedobór {r['short_kwh']} kWh"
            )
    print("\n## System:", json.dumps(ranking["production"], ensure_ascii=False))


def _signed(value: float | None) -> str:
    return "–" if value is None else f"{value:+g}"


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        sys.exit(__doc__)
    main(
        Path(sys.argv[1]),
        date.fromisoformat(sys.argv[2]),
        date.fromisoformat(sys.argv[3]),
        Path(sys.argv[4]) if len(sys.argv) == 5 else None,
    )
