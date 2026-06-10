"""Forecast window for mowing — the dry stretch bounded by next rain / non-work.

Pure domain (no hass). Mirrors the window logic of the legacy Jinja
`sensor.luba_mowing_planner` (home-assistant-config configuration.yaml) so the
Python port can run in parallel and be compared for parity before cutover.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


@dataclass(frozen=True)
class ForecastWindow:
    """Dry mowing window [start, end].

    `end` is bounded by the next rainy slot or the non-work start; `bound` says
    which (or NONE when there is no usable window).
    """

    start: datetime | None
    end: datetime | None
    bound: WindowBound

    @classmethod
    def from_slots(
        cls,
        slots: list[ForecastSlot],
        now: datetime,
        non_work_start: datetime | None,
        rain_prob_threshold: int,
    ) -> ForecastWindow:
        start = cls._window_start(slots, now, rain_prob_threshold)
        end = cls._window_end(slots, start, rain_prob_threshold)
        return cls._clip_to_non_work(start, end, non_work_start)

    @staticmethod
    def _window_start(
        slots: list[ForecastSlot], now: datetime, threshold: int
    ) -> datetime | None:
        covering = next((s for s in slots if s.covers(now)), None)
        # No slot covers now → legacy drops past slots, so treat now as dry.
        raining_now = covering is not None and covering.rain_prob >= threshold
        if not raining_now:
            return now
        # Raining now → the window starts when it next turns dry.
        for s in slots:
            if s.start > now and s.rain_prob < threshold:
                return s.start
        return None

    @staticmethod
    def _window_end(
        slots: list[ForecastSlot], start: datetime | None, threshold: int
    ) -> datetime | None:
        if start is None:
            return None
        # The window ends where the next rainy slot begins. The slot covering
        # `start` is dry by construction, so strict `>` skips nothing.
        for s in slots:
            if s.start > start and s.rain_prob >= threshold:
                return s.start
        return None

    @classmethod
    def _clip_to_non_work(
        cls,
        start: datetime | None,
        end: datetime | None,
        non_work_start: datetime | None,
    ) -> ForecastWindow:
        if start is None:
            return cls(None, None, WindowBound.NONE)
        if non_work_start is not None and (end is None or end > non_work_start):
            return cls(start, non_work_start, WindowBound.NON_WORK)
        if end is not None:
            return cls(start, end, WindowBound.RAIN)
        return cls(start, None, WindowBound.NONE)


@dataclass(frozen=True)
class ForecastSlot:
    """Normalized forecast bucket — domain VO, NOT raw wetteronline.

    The infrastructure mapper builds these from wetteronline's `nowcast_15min`
    (15-min) or hourly (60-min) entries, so the domain works on uniform slots
    (anti-corruption: the domain never sees the wetteronline payload shape).
    """

    start: datetime
    rain_prob: int
    duration: timedelta

    @property
    def end(self) -> datetime:
        return self.start + self.duration

    def covers(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


class WindowBound(StrEnum):
    """What ends the dry mowing window (or that there is none).

    A property of the window itself — orthogonal to the planner's start
    decision (see StartStrategy). Do not conflate the two.
    """

    NONE = "none"
    NON_WORK = "non_work"
    RAIN = "rain"
