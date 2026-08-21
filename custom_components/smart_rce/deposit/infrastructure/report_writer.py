"""Debug mirror of the report as a static JSON file under `www/`.

Temporary (ADR-025 #7): the dashboard reads the WebSocket command, but a file at
`/local/depozyt.json` is trivially inspectable from a browser while the card is
being built. Delete this adapter — and its call in the factory — once the
WebSocket path has proven itself.

Failure here must never matter: it is a debug aid, so the writer swallows and
logs its own errors rather than propagating them into setup.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_FILENAME: Final = "www/depozyt.json"

_LOGGER = logging.getLogger(__name__)


async def async_write_debug_report(hass: HomeAssistant, report: dict[str, Any]) -> None:
    """Mirror `report` to `www/depozyt.json`, best effort."""
    path = Path(hass.config.path(_FILENAME))
    try:
        await hass.async_add_executor_job(_write, path, report)
    except OSError as err:  # debug aid only — never break setup over it
        _LOGGER.warning("Could not write %s: %s", path, err)


def _write(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
