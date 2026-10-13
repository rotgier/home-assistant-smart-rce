#!/usr/bin/env python3
"""Rebuild `archive/energy` day files from a raw recorder dump (offline).

For days the HA recorder no longer holds in full, but a raw dump taken earlier
does: `stats5_YYYY-MM-DD.json.gz` files as written by home-assistant-ops
`research/2026-10-11-backtest-poranne-ladowanie/fetch_recorder_dump.py`
(WebSocket `recorder/statistics_during_period`, `start` in epoch milliseconds).

Uses the production `EnergyDay` and `DailyJsonlArchive`, so the files are
identical in shape to what HA writes, with `source: "recorder_dump"`.

Usage:
    .venv/bin/python scripts/energy_archive_from_dump.py DUMP_DIR OUT_DIR [DAY ...]

Without DAY arguments every day in DUMP_DIR is converted. Copy the resulting
`OUT_DIR/YYYY/YYYY-MM-DD.jsonl.gz` files to
`/config/smart_rce/archive/energy/YYYY/` on HA.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
import gzip
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

# Run as a plain script: make the repo root (custom_components/) importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.smart_rce.domain.energy_day import EnergyDay  # noqa: E402
from custom_components.smart_rce.infrastructure.daily_jsonl_archive import (  # noqa: E402
    DailyJsonlArchive,
)
from custom_components.smart_rce.infrastructure.energy_archive_writer import (  # noqa: E402
    ENERGY_SERIES,
)

_TZ = ZoneInfo("Europe/Warsaw")


def main(dump_dir: Path, out_dir: Path, days: list[date]) -> None:
    archive = DailyJsonlArchive(out_dir)
    files = sorted(dump_dir.glob("stats5_*.json.gz"))
    for path in files:
        day = date.fromisoformat(path.name.removeprefix("stats5_")[:10])
        if days and day not in days:
            continue
        stats = json.loads(gzip.decompress(path.read_bytes()))
        readings = {
            name: {
                datetime.fromtimestamp(row["start"] / 1000, tz=UTC): row.get(field)
                for row in stats.get(statistic_id, [])
            }
            for statistic_id, (name, field) in ENERGY_SERIES.items()
        }
        energy = EnergyDay.from_readings(day, _TZ, readings, source="recorder_dump")
        filled = {name: energy.filled_slots(name) for name in energy.series}
        if not any(filled.values()):
            print(f"{day}: no data, skipped")
            continue
        archive.write_day(day, [energy.to_dict()])
        print(f"{day}: {energy.slot_count} slots, filled {filled}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(
        Path(sys.argv[1]),
        Path(sys.argv[2]),
        [date.fromisoformat(arg) for arg in sys.argv[3:]],
    )
