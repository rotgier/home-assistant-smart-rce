"""WeatherDiffWriter — driven adapter zapisujący weather forecast diffs do plików.

Zapisuje formatted diff text (zwracany przez `WeatherForecastHistory.update_from_forecast`)
do plików w `<config_dir>/smart_rce/forecast_{initial,diff}_<timestamp>.txt`.
Używane do diagnostyki — historyczny ślad jak forecast się zmieniał w ciągu dnia.

Hexagonal pattern: **driven adapter (outbound)** — domain dictates "save this
diff", konkretna impl używa filesystem (HA config_dir + aiofiles).

Wired w `pv_forecast_factory` jako side-effect history update — gdy
update_from_forecast zwraca (diff_text, is_initial), factory schedule
`async writer.write(diff_text, is_initial, now)` task.
"""

from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path

import aiofiles  # type: ignore[import-untyped]

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class WeatherDiffWriter:
    """Writes formatted weather forecast diffs do <config>/smart_rce/forecast_*.txt."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._dir = Path(hass.config.config_dir) / "smart_rce"

    async def write(self, diff_text: str, is_initial: bool, now: datetime) -> None:
        tag = "initial" if is_initial else "diff"
        filename = f"forecast_{tag}_{now.strftime('%Y-%m-%dT%H:%M')}.txt"
        path = self._dir / filename
        try:
            async with aiofiles.open(path, mode="w", encoding="utf-8") as f:
                await f.write(diff_text)
        except OSError:
            _LOGGER.exception("Failed to write weather diff to %s", path)
