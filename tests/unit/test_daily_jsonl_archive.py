"""DailyJsonlArchive: durable appends, torn-line tolerance, atomic day close."""

from __future__ import annotations

from datetime import date
import gzip
from pathlib import Path

from custom_components.smart_rce.infrastructure.daily_jsonl_archive import (
    DailyJsonlArchive,
)

_DAY = date(2026, 10, 13)
_TODAY = date(2026, 10, 14)


def test_append_and_read_back(tmp_path: Path) -> None:
    archive = DailyJsonlArchive(tmp_path)
    archive.append(_DAY, {"n": 1, "x": 0.1})
    archive.append(_DAY, {"n": 2, "nested": {"a": [1, 2]}})

    assert archive.read_day(_DAY) == [
        {"n": 1, "x": 0.1},
        {"n": 2, "nested": {"a": [1, 2]}},
    ]
    assert (tmp_path / "2026" / "2026-10-13.jsonl").exists()


def test_missing_day_reads_empty(tmp_path: Path) -> None:
    archive = DailyJsonlArchive(tmp_path)
    assert archive.read_day(_DAY) == []
    assert not archive.has_day(_DAY)


def test_torn_last_line_is_skipped(tmp_path: Path) -> None:
    """A crash mid-append leaves half a line; the rest of the day survives."""
    archive = DailyJsonlArchive(tmp_path)
    archive.append(_DAY, {"n": 1})
    with (tmp_path / "2026" / "2026-10-13.jsonl").open("ab") as file:
        file.write(b'{"n": 2, "trunc')

    assert archive.read_day(_DAY) == [{"n": 1}]


def test_close_compresses_past_days_only(tmp_path: Path) -> None:
    archive = DailyJsonlArchive(tmp_path)
    archive.append(_DAY, {"n": 1})
    archive.append(_TODAY, {"n": 2})

    assert archive.close_days_before(_TODAY) == [_DAY]

    year = tmp_path / "2026"
    assert not (year / "2026-10-13.jsonl").exists()
    assert gzip.decompress((year / "2026-10-13.jsonl.gz").read_bytes()) == b'{"n":1}\n'
    assert (year / "2026-10-14.jsonl").exists()  # today stays open
    assert not list(year.glob("*.tmp"))
    assert archive.read_day(_DAY) == [{"n": 1}]
    assert archive.has_day(_DAY)


def test_closing_twice_gives_identical_bytes(tmp_path: Path) -> None:
    """Deterministic gzip: a re-closed day dedups in backups."""
    archive = DailyJsonlArchive(tmp_path)
    archive.write_day(_DAY, [{"n": 1}])
    first = (tmp_path / "2026" / "2026-10-13.jsonl.gz").read_bytes()
    archive.write_day(_DAY, [{"n": 1}])
    assert (tmp_path / "2026" / "2026-10-13.jsonl.gz").read_bytes() == first


def test_plain_file_left_by_a_crash_wins_and_is_re_closed(tmp_path: Path) -> None:
    """Crash after publishing .gz but before deleting .jsonl: plain is the source."""
    archive = DailyJsonlArchive(tmp_path)
    archive.write_day(_DAY, [{"n": 1}])
    archive.append(_DAY, {"n": 1})
    archive.append(_DAY, {"n": 2})

    assert archive.read_day(_DAY) == [{"n": 1}, {"n": 2}]
    archive.close_days_before(_TODAY)
    assert archive.read_day(_DAY) == [{"n": 1}, {"n": 2}]
    assert not (tmp_path / "2026" / "2026-10-13.jsonl").exists()


def test_write_day_replaces_an_open_file(tmp_path: Path) -> None:
    archive = DailyJsonlArchive(tmp_path)
    archive.append(_DAY, {"old": True})
    archive.write_day(_DAY, [{"new": True}])
    assert archive.read_day(_DAY) == [{"new": True}]
