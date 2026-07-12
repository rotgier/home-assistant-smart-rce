"""Rain reader — is it raining now, per the wetteronline weather entity.

Driving adapter: owns hass + the weather entity ids. `is_raining_now()` mirrors
the legacy Jinja mute condition exactly (parity): weather state contains
`rain`/`pour`/`lightning` AND precipitation probability > 70%. Behind this port
so a future ground-truth rain gauge (ESP) swaps in as a reader change, not a
logic change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_WET_TOKENS: Final = ("rain", "pour", "lightning")
_PRECIP_THRESHOLD: Final = 70.0


class RainReader:
    """Reads + watches whether it is currently raining (owns hass + entity ids)."""

    _WEATHER: Final[str] = "weather.wetteronline"
    _PRECIP: Final[str] = "sensor.wetteronline_precipitation_probability"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def is_raining_now(self) -> bool:
        """Return True when weather is wet AND precip probability > 70%."""
        weather = self._read(RainReader._WEATHER)
        if weather is None:
            return False
        wet = any(token in weather.lower() for token in _WET_TOKENS)
        return wet and self._read_float(RainReader._PRECIP) > _PRECIP_THRESHOLD

    def _read(self, entity_id: str) -> str | None:
        state = self._hass.states.get(entity_id)
        return state.state if state else None

    def _read_float(self, entity_id: str) -> float:
        raw = self._read(entity_id)
        try:
            return float(raw) if raw is not None else 0.0
        except ValueError:
            return 0.0
