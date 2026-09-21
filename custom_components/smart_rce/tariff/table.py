"""Loads the shipped tariff table and answers what a kWh costs today.

The evening planner needs one thing from the tariff — the gross cost of
drawing a kWh in a given zone — and needs it without knowing about billing
months or settlement. This is that view: the most recent published rates,
read once and cached, since a tariff changes a few times a year at most.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
import json
from pathlib import Path
from typing import Final

from .rates import Zone, ZoneRates

_TABLE: Final = Path(__file__).parent / "tariff_table.json"


@lru_cache(maxsize=1)
def latest_rates() -> ZoneRates:
    """Rates of the most recent month in the table."""
    rows = json.loads(_TABLE.read_text(encoding="utf-8"))["months"]
    row = max(rows, key=lambda r: r["month"])
    return ZoneRates(
        energy={zone: row["energy"][zone.value] for zone in Zone},
        distribution={zone: row["distribution"][zone.value] for zone in Zone},
    )


@lru_cache(maxsize=1)
def latest_month() -> str:
    """`YYYY-MM` of the most recent row — what the rates above describe."""
    rows = json.loads(_TABLE.read_text(encoding="utf-8"))["months"]
    return str(max(r["month"] for r in rows))


def covers(day: date) -> bool:
    """Tell whether the table has rates published for `day`'s month or later.

    False means the shipped table predates the day being priced, so the rates
    in use are stale — which happens every January until a new URE decision is
    entered.
    """
    return latest_month() >= f"{day.year:04d}-{day.month:02d}"
