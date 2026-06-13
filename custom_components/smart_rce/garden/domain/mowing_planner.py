"""Mowing planner — if/when to start Luba.

Decides from battery, task progress, dock state and the forecast window.
Pure domain (no hass). Mirrors the legacy Jinja `sensor.luba_mowing_planner`.

Two start strategies once a usable window exists:
- ASAP: window shorter than needed → start now, mow what we can before rain.
- LAZY: window long enough → start late so we finish near the window end (mow
  on the freshest forecast, minimize weather-change risk).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from custom_components.smart_rce.garden.domain.forecast_window import (
    ForecastSlot,
    ForecastWindow,
    WindowBound,
)
from custom_components.smart_rce.garden.domain.non_work import NonWorkHours


class MowingPlanner:
    """Decides start timing. Stateless policy holder (domain constants)."""

    MOWING_RATE: Final = 0.55  # battery pp consumed per minute of mowing
    PROGRESS_RATE: Final = 0.4  # task % gained per minute of mowing
    BATT_FLOOR: Final = 15  # min SoC we allow draining to
    BATT_MIN_START: Final = 30  # min SoC to start a session
    WIN_MIN: Final = 30  # shortest worthwhile window (minutes)
    RAIN_PROB: Final = 50  # precipitation probability threshold (%)
    END_BUFFER: Final = timedelta(minutes=10)  # need >10 min left to start

    def decide(self, inp: MowingInput) -> PlannerDecision:
        # The window cannot open before the latest of: now, the end of an
        # active quiet window (legacy Jinja missed this — it only clipped to
        # the NEXT quiet start), and `dry_at` (grass dry-out after the last
        # rain). All three are floors on when mowing may begin.
        non_work_start = inp.non_work.next_start(inp.now) if inp.non_work else None
        quiet_until = (
            inp.non_work.end_of_active_window(inp.now) if inp.non_work else None
        )
        from_moment = inp.now
        if quiet_until is not None:
            from_moment = max(from_moment, quiet_until)
        if inp.dry_at is not None:
            from_moment = max(from_moment, inp.dry_at)
        window = ForecastWindow.from_slots(
            inp.slots, from_moment, non_work_start, self.RAIN_PROB
        )
        time_to_drain = self._time_to_drain(inp.battery)
        time_to_finish = self._time_to_finish(inp.progress, time_to_drain)
        needed = min(time_to_drain, time_to_finish)

        strategy, opt_start, win_min = self._resolve_start(window, needed)
        should = self._should_start(inp, window, opt_start)

        return PlannerDecision(
            should_start=should,
            window_start=window.start,
            deadline=window.end,
            opt_start=opt_start,
            window_bound=window.bound,
            strategy=strategy,
            needed_min=needed,
            window_min=win_min,
            time_to_drain_min=time_to_drain,
            time_to_finish_min=time_to_finish,
            battery=inp.battery,
            progress=inp.progress,
            at_dock=inp.at_dock,
        )

    def _time_to_drain(self, battery: int) -> int:
        if battery <= self.BATT_FLOOR:
            return 0
        return round((battery - self.BATT_FLOOR) / self.MOWING_RATE)

    def _time_to_finish(self, progress: int, time_to_drain: int) -> int:
        if progress <= 0:
            return time_to_drain
        return round((100 - progress) / self.PROGRESS_RATE)

    def _resolve_start(
        self, window: ForecastWindow, needed: int
    ) -> tuple[StartStrategy, datetime | None, int]:
        """Pick the start strategy. Returns (strategy, opt_start, window_min)."""
        if window.start is None or window.end is None or window.end <= window.start:
            return StartStrategy.NO_WINDOW, None, 0

        win_min = round((window.end - window.start).total_seconds() / 60)
        if win_min < self.WIN_MIN:
            return StartStrategy.SKIP_SHORT_WINDOW, None, win_min
        if win_min < needed:
            return StartStrategy.ASAP, window.start, win_min
        return StartStrategy.LAZY, window.end - timedelta(minutes=needed), win_min

    def _should_start(
        self, inp: MowingInput, window: ForecastWindow, opt_start: datetime | None
    ) -> bool:
        if opt_start is None or window.end is None:
            return False
        return (
            inp.now >= opt_start
            and inp.now < window.end - self.END_BUFFER
            and inp.battery >= self.BATT_MIN_START
            and inp.at_dock
        )


@dataclass(frozen=True)
class MowingInput:
    """Snapshot the planner decides on.

    Extend this (not the method signature) when a new input is needed.
    """

    battery: int
    progress: int
    at_dock: bool
    now: datetime
    slots: list[ForecastSlot]
    non_work: NonWorkHours | None  # planner derives next start / active end
    dry_at: datetime | None = None  # grass dry-out floor (rain_ended + dry_hours)


@dataclass(frozen=True)
class PlannerDecision:
    """Planner output (pure domain VO).

    Keeps the two orthogonal dimensions separate: `window_bound` (what ends the
    window) and `strategy` (what the planner decided). HA serialization is the
    sensor layer's job (`dataclasses.asdict` over these descriptive fields).
    """

    should_start: bool
    window_start: datetime | None
    deadline: datetime | None
    opt_start: datetime | None
    window_bound: WindowBound
    strategy: StartStrategy
    needed_min: int
    window_min: int
    time_to_drain_min: int
    time_to_finish_min: int
    battery: int
    progress: int
    at_dock: bool


class StartStrategy(StrEnum):
    """What the planner decided about starting (orthogonal to WindowBound)."""

    NO_WINDOW = "no_window"
    SKIP_SHORT_WINDOW = "skip_short_window"
    ASAP = "asap"
    LAZY = "lazy"
