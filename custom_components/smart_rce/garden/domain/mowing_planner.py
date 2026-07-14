"""Mowing planner — if/when to start Luba.

Decides from battery, task progress, dock state and the forecast window.
Pure domain (no hass). Mirrors the legacy Jinja `sensor.luba_mowing_planner`.

Start strategies once a usable window exists:
- ASAP: window shorter than what we could mow → start now, grab what we can
  before rain (battery- or rain-bound, whichever is shorter).
- WAIT_BATTERY: window fits the task but the battery would not outlast it by
  BATTERY_RESERVE_MIN → stay docked and charge (flips to GO as it charges). The
  firmware auto-resumes a paused task at ~90% on its own, so we normally WAIT and
  let it — EXCEPT when the battery has climbed past `FIRMWARE_RESUME_SOC` while
  still docked (firmware stalled after a manual recall): then HA resumes, so a
  task too big for one charge is not stuck at full battery forever.
- GO: window fits AND battery finishes the task with reserve → start at the
  window open (earliest), finishing in one charge. Earliest start banks the most
  lawn before the window can shrink (early rain or the non-work boundary).

Fresh start (no task in progress, progress == 0) has no finish estimate, so the
resume reserve logic does not apply: a wide window waits until the battery reaches
the fresh-start threshold (`fresh_start_battery`, default 90) then GO at the open;
a window shorter than the battery endurance is ASAP (grab what we can). Whatever a
single charge cannot finish is left to Luba's own post-charge auto-resume.
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

    MOWING_RATE: Final = 0.62  # battery pp consumed per minute of mowing
    PROGRESS_RATE: Final = 0.4  # task %/min — linear finish fallback
    BATT_FLOOR: Final = 15  # min SoC we allow draining to
    BATT_MIN_START: Final = 30  # min SoC to start a session
    WIN_MIN: Final = 30  # shortest worthwhile window (minutes)
    RAIN_PROB: Final = 50  # precipitation probability threshold (%)
    END_BUFFER: Final = timedelta(minutes=10)  # need >10 min left to start
    BATTERY_RESERVE_MIN: Final = 20  # battery must outlast the task by this (min)
    RESUME_GRACE: Final = timedelta(minutes=10)  # hold HA start after quiet end
    FIRMWARE_RESUME_SOC: Final = 91  # firmware auto-resumes a paused task ~90%;
    # above this AND still docked ⇒ firmware stalled (manual recall) → HA resumes
    DEFAULT_FRESH_BATTERY: Final = 90  # fresh-start SoC threshold (tunable via number)

    def decide(self, inp: MowingInput) -> PlannerDecision:
        non_work_start = inp.non_work.next_start(inp.now) if inp.non_work else None
        window = ForecastWindow.from_slots(
            inp.slots, self._earliest_start(inp), non_work_start, self.RAIN_PROB
        )
        drain = self._time_to_drain(inp.battery)
        finish = self._time_to_finish(inp.progress, drain, inp.time_left_min)
        strategy, opt_start, win_min = self._resolve_start(inp, window, finish, drain)
        return PlannerDecision(
            should_start=self._should_start(inp, window, opt_start),
            window_start=window.start,
            window_end=window.end,
            opt_start=opt_start,
            window_bound=window.bound,
            strategy=strategy,
            needed_min=min(drain, finish),
            window_min=win_min,
            time_to_drain_min=drain,
            time_to_finish_min=finish,
            battery=inp.battery,
            progress=inp.progress,
            at_dock=inp.at_dock,
        )

    def _earliest_start(self, inp: MowingInput) -> datetime:
        """Floor on when mowing may begin.

        The latest of: now, the end of an active quiet window, `dry_at` (grass
        dry-out after the last rain) and `manual_until` (a manual park). Both
        holds clamp the window the same way, so the planner never dispatches a
        start into rain OR a user-requested park. The active-quiet-end floor is
        what the legacy Jinja missed — it clipped only to the NEXT quiet start.
        """
        floor = inp.now
        if inp.non_work is not None:
            quiet_until = inp.non_work.end_of_active_window(inp.now)
            if quiet_until is not None:
                floor = max(floor, quiet_until)
        if inp.dry_at is not None:
            floor = max(floor, inp.dry_at)
        if inp.manual_until is not None:
            floor = max(floor, inp.manual_until)
        return floor

    def _time_to_drain(self, battery: int) -> int:
        if battery <= self.BATT_FLOOR:
            return 0
        return round((battery - self.BATT_FLOOR) / self.MOWING_RATE)

    def _time_to_finish(
        self, progress: int, time_to_drain: int, time_left: int | None
    ) -> int:
        # No task in progress → no finish estimate; fresh-start logic owns this
        # case (parity hack: report drain so needed == drain).
        if progress <= 0:
            return time_to_drain
        # Prefer the firmware's own remaining estimate (accounts for geometry,
        # speed, blade) — the linear PROGRESS_RATE model is only a fallback when
        # the sensor is unavailable / not yet reporting.
        if time_left is not None and time_left > 0:
            return time_left
        return round((100 - progress) / self.PROGRESS_RATE)

    def _resolve_start(
        self, inp: MowingInput, window: ForecastWindow, finish: int, drain: int
    ) -> tuple[StartStrategy, datetime | None, int]:
        """Pick the start strategy + opt_start. Returns (strategy, opt_start, win_min).

        Window viability first (none / too short / shorter-than-we-could-mow →
        ASAP), then the fresh-start vs resume policy.
        """
        if window.start is None or window.end is None or window.end <= window.start:
            return StartStrategy.NO_WINDOW, None, 0
        win_min = round((window.end - window.start).total_seconds() / 60)
        if win_min < self.WIN_MIN:
            return StartStrategy.SKIP_SHORT_WINDOW, None, win_min
        if win_min < min(finish, drain):
            # Window shorter than we could mow → grab what we can before it closes.
            return StartStrategy.ASAP, window.start, win_min
        if inp.progress <= 0:
            return self._resolve_fresh(inp, window.start, win_min)
        return self._resolve_resume(inp, window.start, win_min, finish, drain)

    def _resolve_fresh(
        self, inp: MowingInput, start: datetime, win_min: int
    ) -> tuple[StartStrategy, datetime | None, int]:
        """Fresh start: GO at the fresh-start battery threshold, else charge.

        No task in progress, so a full-ish charge banks a long stretch before
        the first run; whatever one charge cannot finish is left to the
        firmware's own post-charge auto-resume.
        """
        if inp.battery >= inp.fresh_start_battery:
            return StartStrategy.GO, start, win_min
        return StartStrategy.WAIT_BATTERY, None, win_min

    def _resolve_resume(
        self, inp: MowingInput, start: datetime, win_min: int, finish: int, drain: int
    ) -> tuple[StartStrategy, datetime | None, int]:
        """Resume an in-progress task.

        GO when the battery outlasts the remaining task by `BATTERY_RESERVE_MIN`
        (finish in one charge; earliest start banks the most lawn before the
        window shrinks). Otherwise the firmware normally auto-resumes at ~90% on
        its own, so WAIT and let it — resuming at a partial charge means a short
        run + extra dock trips. EXCEPT after a MANUAL recall the firmware will NOT
        auto-resume: detected by the battery climbing past `FIRMWARE_RESUME_SOC`
        while still docked (the `at_dock` gate is in `_should_start`), so a task
        too big for one charge would be stuck at full battery forever — there HA
        resumes. (Timing-side half of this firmware-fallback policy:
        `_firmware_resume_grace`.)
        """
        if drain >= finish + self.BATTERY_RESERVE_MIN:
            return StartStrategy.GO, start, win_min
        if inp.battery > self.FIRMWARE_RESUME_SOC:
            return StartStrategy.GO, start, win_min
        return StartStrategy.WAIT_BATTERY, None, win_min

    def _should_start(
        self, inp: MowingInput, window: ForecastWindow, opt_start: datetime | None
    ) -> bool:
        """Is NOW the moment to fire — given the resolved strategy's opt_start."""
        if opt_start is None or window.end is None:
            return False
        if self._firmware_resume_grace(inp):
            return False
        return (
            inp.now >= opt_start
            and inp.now < window.end - self.END_BUFFER
            and inp.battery >= self.BATT_MIN_START
            and inp.at_dock
        )

    def _firmware_resume_grace(self, inp: MowingInput) -> bool:
        """Whether the post-quiet-end grace is active (hold HA, let firmware win).

        Right after the quiet-end the firmware auto-resumes its IN-PROGRESS task
        on its own; we hold HA for `RESUME_GRACE` so we don't race it with a
        duplicate cloud command. If it hasn't resumed by then (still docked),
        `_should_start` fires HA as the fallback. Only a resume (progress > 0) —
        a fresh start has no task to auto-resume, so it fires right at the
        quiet-end. Bites only just after the non-work end; mid-day windows are
        unaffected. (Strategy-side half of this firmware-fallback policy: the
        `FIRMWARE_RESUME_SOC` branch in `_resolve_resume`.)
        """
        return (
            inp.progress > 0
            and inp.non_work is not None
            and inp.now < inp.non_work.recent_end(inp.now) + self.RESUME_GRACE
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
    manual_until: datetime | None = None  # manual-park floor (mowing hold)
    time_left_min: int | None = None  # firmware remaining estimate (progress>0)
    fresh_start_battery: int = 90  # SoC threshold for fresh GO (DEFAULT_FRESH_BATTERY)


@dataclass(frozen=True)
class PlannerDecision:
    """Planner output (pure domain VO).

    Keeps the two orthogonal dimensions separate: `window_bound` (what ends the
    window) and `strategy` (what the planner decided). HA serialization is the
    sensor layer's job (`dataclasses.asdict` over these descriptive fields).
    """

    should_start: bool
    window_start: datetime | None
    window_end: datetime | None
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
    WAIT_BATTERY = "wait_battery"
    GO = "go"
