"""ForecastScoreArchive — scored days, one JSON Lines file per day (`archive/score`).

A day collects several records: preliminary (16:06), final (21:06), a
replacing final from the 00:20 check, rescoring runs. The reader takes the last
final (`DayScore.latest_of`). The day is closed (gzipped) after its check;
a later rescore of a closed day rewrites the file whole — never an append to
the gzip (see `DailyJsonlArchive`). Blocking I/O: call through the executor.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from .daily_jsonl_archive import DailyJsonlArchive

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

# Under /config, next to the forecast and energy archives (the HA backup has it).
SCORE_ARCHIVE_DIR: Final = "smart_rce/archive/score"


class ForecastScoreArchive:
    """Appends score records and reads a day back."""

    def __init__(self, archive: DailyJsonlArchive) -> None:
        self._archive = archive

    @classmethod
    def in_config(cls, hass: HomeAssistant) -> ForecastScoreArchive:
        return cls(DailyJsonlArchive(Path(hass.config.path(SCORE_ARCHIVE_DIR))))

    def add(self, day: date, record: Mapping[str, Any]) -> None:
        """Append to an open day; rewrite a closed one with the record added."""
        if self._archive.is_closed(day):
            self._archive.write_day(day, [*self._archive.read_day(day), record])
        else:
            self._archive.append(day, record)

    def records(self, day: date) -> list[dict[str, Any]]:
        return self._archive.read_day(day)

    def close_days_before(self, today: date) -> list[date]:
        return self._archive.close_days_before(today)
