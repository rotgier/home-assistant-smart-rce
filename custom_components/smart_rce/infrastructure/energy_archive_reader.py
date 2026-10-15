"""EnergyArchiveReader — archived days of measured energy, for production code.

The 5-minute statistics live 10 days in the recorder; 8 previous workdays reach
up to 12 calendar days back, 5 previous Saturdays five weeks. Loaders take a day
from the archive and fall back to the recorder only for days it lacks (today,
yesterday before 00:15, days before the archive started on 03.10.2026).
"""

from __future__ import annotations

from datetime import date
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Final

from ..domain.energy_day import EnergyDay
from .daily_jsonl_archive import DailyJsonlArchive

if TYPE_CHECKING:
    from collections.abc import Iterable

    from homeassistant.core import HomeAssistant

# Under /config: the daily HA backup includes it (/share it does not).
ENERGY_ARCHIVE_DIR: Final = "smart_rce/archive/energy"

_LOGGER = logging.getLogger(__name__)


class EnergyArchiveReader:
    """Reads archived `EnergyDay`s; a day it cannot read counts as missing."""

    def __init__(self, hass: HomeAssistant, archive: DailyJsonlArchive) -> None:
        self._hass = hass
        self._archive = archive

    @classmethod
    def in_config(cls, hass: HomeAssistant) -> EnergyArchiveReader:
        return cls(hass, DailyJsonlArchive(Path(hass.config.path(ENERGY_ARCHIVE_DIR))))

    async def read_days(self, days: Iterable[date]) -> dict[date, EnergyDay]:
        """Return the archived days among `days` (one executor job for all)."""
        return await self._hass.async_add_executor_job(self._read_days, list(days))

    def _read_days(self, days: list[date]) -> dict[date, EnergyDay]:
        found: dict[date, EnergyDay] = {}
        for day in days:
            try:
                energy = EnergyDay.from_records(self._archive.read_day(day))
            except Exception:  # noqa: BLE001 — a broken file is a missing day
                _LOGGER.exception("Energy archive: %s unreadable", day)
                continue
            if energy is not None:
                found[day] = energy
        return found
