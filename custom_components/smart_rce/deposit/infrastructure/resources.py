"""Loaders for the two JSON resources shipped with the deposit context.

`seed_history.json` is the settled history handed over once from the standalone
calculator (ADR-025 #6) — it is validated against actual invoices, so the
integration replays it rather than re-deriving it. `tariff_table.json` holds the
per-zone rates read off those invoices; it needs a manual refresh when a new
tariff takes effect, which `Tariff` tolerates by clamping to the nearest month.

Both do blocking file reads — call them from an executor when inside HA.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Final

from ..domain.billing_month import BillingMonth
from ..domain.reference_year import MonthRecord
from ..domain.tariff import Tariff, Zone, ZoneRates

_RESOURCE_DIR: Final = Path(__file__).parent
_SEED_HISTORY: Final = _RESOURCE_DIR / "seed_history.json"
_TARIFF_TABLE: Final = _RESOURCE_DIR / "tariff_table.json"


def load_seed_history() -> SeedHistory:
    """Settled months (oldest first) plus the partially elapsed month, as measured."""
    data = _read(_SEED_HISTORY)
    partial = data.get("partial")
    return SeedHistory(
        months=[_record(row) for row in data["months"]],
        partial=_record(partial) if partial else None,
        partial_elapsed_days=partial["elapsed_days"] if partial else 0,
    )


@dataclass(frozen=True)
class SeedHistory:
    """What the calculator handed over.

    The partial month stays exactly as measured, not extrapolated — scaling it to
    a full month is a modelling decision that belongs to the projection, not to a
    file loader.
    """

    months: list[MonthRecord]
    partial: MonthRecord | None
    partial_elapsed_days: int


def _record(row: dict[str, Any]) -> MonthRecord:
    return MonthRecord(
        month=BillingMonth.parse(row["month"]),
        exported_kwh=row["exported_kwh"],
        deposit_earned=row["deposit_earned"],
        import_kwh={zone: row["import_kwh"][zone.value] for zone in Zone},
    )


def load_tariff() -> Tariff:
    return Tariff(
        {
            BillingMonth.parse(row["month"]): ZoneRates(
                energy={zone: row["energy"][zone.value] for zone in Zone},
                distribution={zone: row["distribution"][zone.value] for zone in Zone},
            )
            for row in _read(_TARIFF_TABLE)["months"]
        }
    )


def _read(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data: dict[str, Any] = json.load(handle)
    return data
