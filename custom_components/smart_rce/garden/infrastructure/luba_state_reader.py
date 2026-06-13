"""Luba state reader — mower telemetry from hass → planner inputs.

Driving adapter for the mowing planner: owns the hass handle and the mammotion
entity ids (the application layer stays unaware of both). Defaults mirror the
legacy Jinja planner exactly (parity): unavailable battery/progress read as 0,
`at_dock` is `lawn_mower == docked OR charging == on` (the lawn_mower entity
alone flaps — charging is the stable dock signal).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from custom_components.smart_rce.garden.const import (
    LUBA_BATTERY_SENSOR,
    LUBA_CHARGING_SENSOR,
    LUBA_LAWN_MOWER,
    LUBA_PROGRESS_SENSOR,
)
from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.const import STATE_ON
from homeassistant.helpers.event import async_track_state_change_event

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import CALLBACK_TYPE, HomeAssistant


class LubaStateReader:
    """Reads + watches Luba telemetry (owns hass and entity ids)."""

    _BATTERY: Final[str] = LUBA_BATTERY_SENSOR
    _PROGRESS: Final[str] = LUBA_PROGRESS_SENSOR
    _LAWN_MOWER: Final[str] = LUBA_LAWN_MOWER
    _CHARGING: Final[str] = LUBA_CHARGING_SENSOR

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def read_battery(self) -> int:
        return self._read_int(LubaStateReader._BATTERY)

    def read_progress(self) -> int:
        return self._read_int(LubaStateReader._PROGRESS)

    def read_at_dock(self) -> bool:
        return (
            self._read_state(LubaStateReader._LAWN_MOWER) == LawnMowerActivity.DOCKED
            or self._read_state(LubaStateReader._CHARGING) == STATE_ON
        )

    def subscribe(self, on_change: Callable[[], None]) -> CALLBACK_TYPE:
        """Invoke `on_change` on any tracked entity change; returns unsubscribe."""
        return async_track_state_change_event(
            self._hass,
            [
                LubaStateReader._BATTERY,
                LubaStateReader._PROGRESS,
                LubaStateReader._LAWN_MOWER,
                LubaStateReader._CHARGING,
            ],
            lambda _event: on_change(),
        )

    def _read_state(self, entity_id: str) -> str | None:
        state = self._hass.states.get(entity_id)
        return state.state if state else None

    def _read_int(self, entity_id: str) -> int:
        raw = self._read_state(entity_id)
        try:
            return int(float(raw)) if raw is not None else 0
        except ValueError:
            return 0
