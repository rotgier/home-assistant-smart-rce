"""DailyJsonlArchive — append-only JSON Lines, one file per local day.

Layout: `<root>/YYYY/YYYY-MM-DD.jsonl` while the day is open, then
`YYYY-MM-DD.jsonl.gz` once it is closed. Plain blocking file I/O — callers on
the event loop go through `hass.async_add_executor_job`.

Why not append straight into the `.gz`: gzip does not survive an interrupted
append (zlib needs a dedicated recovery protocol for that, `examples/gzlog.c`),
and one torn member would cost the whole day. An interrupted append to plain
JSONL costs at most its last line, which `read_day` skips.

Why one file per day and not a database: a closed day never changes again, so
backups copy it once, a day can be rewritten or dropped on its own, and the
archive is readable with nothing but `zcat` (or DuckDB's `read_json`).
"""

from __future__ import annotations

from datetime import date
import gzip
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import orjson

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_PLAIN: Final = ".jsonl"
_CLOSED: Final = ".jsonl.gz"

_LOGGER = logging.getLogger(__name__)


class DailyJsonlArchive:
    """Reads and writes day files under one root directory."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def append(self, day: date, record: Mapping[str, Any]) -> None:
        """Append one record to the open file of `day`, durably (fsync)."""
        path = self._path(day, _PLAIN)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as file:
            file.write(orjson.dumps(record) + b"\n")
            file.flush()
            os.fsync(file.fileno())

    def write_day(self, day: date, records: Iterable[Mapping[str, Any]]) -> None:
        """Write a complete, already closed day in one go (replaces it)."""
        payload = b"".join(orjson.dumps(record) + b"\n" for record in records)
        self._publish_closed(day, payload)
        self._path(day, _PLAIN).unlink(missing_ok=True)

    def read_day(self, day: date) -> list[dict[str, Any]]:
        """Every readable record of `day`; an unparsable (torn) line is skipped.

        The open file wins over the closed one: it only coexists with a closed
        file after a crash between publishing the archive and deleting the
        plain file, and then it is the complete source.
        """
        plain = self._path(day, _PLAIN)
        if plain.exists():
            raw = plain.read_bytes()
        elif (closed := self._path(day, _CLOSED)).exists():
            raw = gzip.decompress(closed.read_bytes())
        else:
            return []
        records: list[dict[str, Any]] = []
        for number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                records.append(orjson.loads(line))
            except orjson.JSONDecodeError:
                _LOGGER.warning("Skipping unreadable line %d of %s", number, day)
        return records

    def has_day(self, day: date) -> bool:
        return self._path(day, _PLAIN).exists() or self._path(day, _CLOSED).exists()

    def close_days_before(self, today: date) -> list[date]:
        """Compress every open file of a day before `today`; return those days.

        Runs after midnight and at startup — a restart can leave yesterday open.
        """
        closed: list[date] = []
        for path in sorted(self._root.glob(f"*/*{_PLAIN}")):
            try:
                day = date.fromisoformat(path.name.removesuffix(_PLAIN))
            except ValueError:
                continue
            if day >= today:
                continue
            self._publish_closed(day, path.read_bytes())
            path.unlink()
            closed.append(day)
        return closed

    def _publish_closed(self, day: date, payload: bytes) -> None:
        """Write `payload` gzipped next to its final name, then swap atomically."""
        target = self._path(day, _CLOSED)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        with tmp.open("wb") as file:
            # mtime=0: closing the same day twice yields identical bytes.
            file.write(gzip.compress(payload, mtime=0))
            file.flush()
            os.fsync(file.fileno())
        tmp.replace(target)

    def _path(self, day: date, suffix: str) -> Path:
        return self._root / f"{day.year:04d}" / f"{day.isoformat()}{suffix}"
