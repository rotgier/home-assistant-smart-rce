"""Mowing planner — if/when to start Luba.

Decides from battery, task progress, dock state and the forecast window.
Pure domain (no hass). Mirrors the legacy Jinja `sensor.luba_mowing_planner`.

Two regimes decide the start, split by whether the WINDOW is the binding
constraint (see `_resolve_start`):

A) Window-limited — the rain / non-work window closes before we could finish the
   task OR drain the battery. The clock is the constraint, not the battery, so:
- ASAP: start now and grab what lawn we can before the window shuts. The battery
  reserve does NOT apply here — a partial run is still worth it before close.

B) Window has room — the decision is about battery vs task, not the clock:
- WAIT_BATTERY: the battery would not outlast the remaining task by
  `FINISH_MARGIN_MIN` → stay docked and charge (flips to GO as it charges). The
  firmware auto-resumes a paused task at ~90% on its own, so we normally WAIT and
  let it — EXCEPT when the battery has climbed past `FIRMWARE_RESUME_SOC` while
  still docked (firmware stalled after a manual recall): then HA resumes, so a
  task too big for one charge is not stuck at full battery forever.
- GO: battery finishes the task with `FINISH_MARGIN_MIN` to spare → start at the
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

    MOWING_RATE: Final = 0.65  # battery pp consumed per minute of mowing
    PROGRESS_RATE: Final = 0.4  # task %/min — linear finish fallback
    BATT_FLOOR: Final = 15  # min SoC we allow draining to
    BATT_MIN_START: Final = 30  # min SoC to start a session
    WIN_MIN: Final = 30  # shortest worthwhile window (minutes)
    RAIN_PROB: Final = 50  # precipitation probability threshold (%)
    END_BUFFER: Final = timedelta(minutes=10)  # need >10 min left to start
    # Battery runtime must beat the finish estimate by this margin (min) before we
    # commit to finishing an in-progress program in ONE charge. The mower's
    # `time_left` runs optimistic, so the margin absorbs that error — without it
    # HA resumes on a partial charge and the mower returns a few % short of done,
    # costing an extra dock trip (2026-07-28: dispatched @55% → 98% → had to redock).
    FINISH_MARGIN_MIN: Final = 32
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
        needed_min = min(drain, finish)
        return PlannerDecision(
            should_start=self._should_start(inp, window, opt_start),
            window_start=window.start,
            window_end=window.end,
            opt_start=opt_start,
            window_bound=window.bound,
            strategy=strategy,
            run_stop_reason=self._run_stop_reason(
                strategy, inp.progress, needed_min, finish
            ),
            needed_min=needed_min,
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

        First reject unusable windows, then split on WHAT binds the start:
        the window (rain/non-work closing → ASAP) vs the battery/task (fresh vs
        finish-in-one-charge). Only the latter consults `FINISH_MARGIN_MIN`.
        """
        if window.start is None or window.end is None or window.end <= window.start:
            return StartStrategy.NO_WINDOW, None, 0
        win_min = round((window.end - window.start).total_seconds() / 60)
        if win_min < self.WIN_MIN:
            return StartStrategy.SKIP_SHORT_WINDOW, None, win_min
        if self._window_cuts_run_short(win_min, finish, drain):
            return StartStrategy.ASAP, window.start, win_min
        if inp.progress <= 0:
            return self._resolve_fresh(inp, window.start, win_min)
        return self._resolve_finish(inp, window.start, win_min, finish, drain)

    def _window_cuts_run_short(self, win_min: int, finish: int, drain: int) -> bool:
        """Whether the window closes before this run would naturally end.

        A run ends when the task finishes OR the battery drains, whichever comes
        first — `min(finish, drain)`. If the rain / non-work window is shorter
        than that, the clock is the binding constraint: start now and grab what
        lawn we can before it shuts (Regime A). The battery `FINISH_MARGIN_MIN`
        does NOT apply here — a partial run still beats none. When this is False
        the window has room and the start is decided on battery vs task
        (Regime B: `_resolve_fresh` / `_resolve_finish`).
        """
        return win_min < min(finish, drain)

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

    def _resolve_finish(
        self, inp: MowingInput, start: datetime, win_min: int, finish: int, drain: int
    ) -> tuple[StartStrategy, datetime | None, int]:
        """Finish an in-progress task (window already has room — Regime B).

        GO when the battery runtime beats the remaining task by `FINISH_MARGIN_MIN`
        (finish in one charge; earliest start banks the most lawn before the
        window shrinks). Otherwise the firmware normally auto-resumes at ~90% on
        its own, so WAIT and let it — resuming at a partial charge means a short
        run + an extra dock trip a few % short of done. EXCEPT after a MANUAL
        recall the firmware will NOT auto-resume: detected by the battery climbing
        past `FIRMWARE_RESUME_SOC` while still docked (the `at_dock` gate is in
        `_should_start`), so a task too big for one charge would be stuck at full
        battery forever — there HA resumes. (Timing-side half of this
        firmware-fallback policy: `_firmware_resume_grace`.)
        """
        if drain >= finish + self.FINISH_MARGIN_MIN:
            return StartStrategy.GO, start, win_min
        if inp.battery > self.FIRMWARE_RESUME_SOC:
            return StartStrategy.GO, start, win_min
        return StartStrategy.WAIT_BATTERY, None, win_min

    @staticmethod
    def _run_stop_reason(
        strategy: StartStrategy, progress: int, needed_min: int, finish: int
    ) -> RunStopReason:
        """Return what would end the dispatched run: window / task-finish / battery drain.

        Orthogonal to ``window_bound`` (which describes what closes the forecast
        window even when the run finishes long before it). Window-limited is exactly
        the ASAP strategy (the ``_window_cuts_run_short`` branch is the only producer
        of ASAP), so it is read off the already-resolved ``strategy`` instead of
        re-testing the window. The finish-vs-battery split is the physical argmin of
        the run's two natural ends: ``needed_min == min(finish, drain)`` picks it —
        finish wins ⇒ the task completes this run, else the battery drains first
        (a fresh start, ``progress <= 0``, has no finish estimate → always battery).

        Meaningful only when a run is dispatched (``should_start``); NO_WINDOW /
        SKIP_SHORT_WINDOW never start, so their reported reason is unused.
        """
        if strategy is StartStrategy.ASAP:
            return RunStopReason.WINDOW
        if progress > 0 and needed_min == finish:
            return RunStopReason.FINISH
        return RunStopReason.BATTERY

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
        `FIRMWARE_RESUME_SOC` branch in `_resolve_finish`.)
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

    Keeps the orthogonal dimensions separate: `window_bound` (what ends the forecast
    window), `strategy` (what the planner decided to do) and `run_stop_reason` (what
    would actually end the dispatched run — task-finish / battery / window). HA
    serialization is the sensor layer's job (`dataclasses.asdict` over these fields).
    """

    should_start: bool
    window_start: datetime | None
    window_end: datetime | None
    opt_start: datetime | None
    window_bound: WindowBound
    strategy: StartStrategy
    run_stop_reason: RunStopReason
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


class RunStopReason(StrEnum):
    """What would end the dispatched run first (orthogonal to WindowBound/StartStrategy).

    Lets a consumer (e.g. the resume notification) say whether this dispatch finishes
    the task or only makes partial progress before docking/window-close.
    """

    FINISH = "finish"  # remaining task completes this run
    BATTERY = "battery"  # battery drains first → dock, firmware/HA resumes after charge
    WINDOW = "window"  # rain / non-work window closes the run early (ASAP dispatch)
