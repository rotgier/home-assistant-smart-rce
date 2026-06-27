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
    LUBA_TIME_LEFT_SENSOR,
)
from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.const import STATE_ON
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant
    from homeassistant.helpers.event import EventStateChangedData


class LubaStateReader:
    """Reads + watches Luba telemetry (owns hass and entity ids)."""

    _BATTERY: Final[str] = LUBA_BATTERY_SENSOR
    _PROGRESS: Final[str] = LUBA_PROGRESS_SENSOR
    _LAWN_MOWER: Final[str] = LUBA_LAWN_MOWER
    _CHARGING: Final[str] = LUBA_CHARGING_SENSOR
    _TIME_LEFT: Final[str] = LUBA_TIME_LEFT_SENSOR

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def read_battery(self) -> int:
        return self._read_int(LubaStateReader._BATTERY)

    def read_progress(self) -> int:
        return self._read_int(LubaStateReader._PROGRESS)

    def read_time_left(self) -> int | None:
        """Firmware's remaining-minutes estimate for the current task.

        `work.progress >> 16` (same value the Mammotion app shows) — more
        accurate than the planner's linear progress model. None when the sensor
        is unavailable so the planner falls back to its linear estimate.
        """
        return self._read_int_optional(LubaStateReader._TIME_LEFT)

    def read_at_dock(self) -> bool:
        return (
            self._read_state(LubaStateReader._LAWN_MOWER) == LawnMowerActivity.DOCKED
            or self._read_state(LubaStateReader._CHARGING) == STATE_ON
        )

    def subscribe(self, on_change: Callable[[], None]) -> CALLBACK_TYPE:
        """Invoke `on_change` on any tracked entity change; returns unsubscribe."""

        @callback
        def _changed(_event: Event[EventStateChangedData]) -> None:
            on_change()

        return async_track_state_change_event(
            self._hass,
            [
                LubaStateReader._BATTERY,
                LubaStateReader._PROGRESS,
                LubaStateReader._LAWN_MOWER,
                LubaStateReader._CHARGING,
                LubaStateReader._TIME_LEFT,
            ],
            _changed,
        )

    def _read_state(self, entity_id: str) -> str | None:
        state = self._hass.states.get(entity_id)
        return state.state if state else None

    def _read_int(self, entity_id: str) -> int:
        value = self._read_int_optional(entity_id)
        return value if value is not None else 0

    def _read_int_optional(self, entity_id: str) -> int | None:
        raw = self._read_state(entity_id)
        if raw is None:
            return None
        try:
            return int(float(raw))
        except ValueError:
            return None
