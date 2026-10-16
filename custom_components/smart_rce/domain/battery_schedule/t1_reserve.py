"""T1SocReserve — SoC the battery should hold for the rest of the T1 window.

Value object resolved from one `TargetSoc` variant: the reading for the
per-bucket 60th percentile (p60) of the earlier workdays' consumption. Decision
14.10.2026 took the median (replay of 04-14.10: the old max(flat, prev without
the worst) overshot by ~5 pp and protected no better than flat); 15.10.2026 p60,
since the median left the reserve short on average over 126 workdays
(`domain/consumption_percentile.py`). Without consumption history it falls
back to the flat reading. The flat and prev-without-worst readings stay as
components for comparison.

One figure, several readers: before 07:00 it is the night charge target of the
CHARGE_MORNING_AUTO slot; 07:00-13:00 the reserve still needed for the rest of
the window (falls as the morning goes), the line a morning discharge must not
cross.

On top of the reading (and its 10% buffer) sits a margin that falls as the
morning firms up (decision 15.10.2026): +15 pp until 07:00, so the night
charge stores a little extra while energy is cheap; +10 pp 07-08, +5 pp 08-09,
none from 09:00. Over 126 workdays the system's reserve fell short by more
than 5 pp on 6 days at 06:11, 3 at 07:41, 2 at 08:41; 15 pp at 06:11 covered
all of them. The morning discharge hands the margin back step by step, one
hour at a time (`value_at`). To be recalibrated from the scoreboard after
~30 days.

The 08-09 step is capped by plain Live (decision 15.10.2026, evening): the
line is min(Soft + 5, Live), i.e. the margin is max(0, min(5, Live - Soft)).
By 08:41 Live has seen most of the morning, so on an ordinary day it agrees
with Soft and the margin goes; it stays on very cloudy days (14.10: Live 42 vs
Soft 23). Scoreboard 05-15.10 at 08:41: Soft + 5 overshot by 4-10 pp, the
capped line by 1-6. The 07-08 step is capped the same way by the highest of
Live, Extrap Proportional and Extrap Blend (decision 16.10.2026; an Extrap
without a result yet is left out): at 07:41 05-15.10 Soft + 10 overshot by
10-14 pp, the capped line by 0-14, typically 1-2, never short. The night step
stays whole — there the margin guards the consumption model, which no PV
variant sees (`margin_pp_for`). Backtest behind the rule: home-assistant-ops
`research/2026-10-11-backtest-poranne-ladowanie/`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from ..target_soc import TargetSoc, TargetSocResult


class ReserveWindow(StrEnum):
    """Which part of the 7-13 window the figure covers."""

    FULL = "full"
    """The whole window ahead — the night before / the evening preview."""

    REMAINING = "remaining"
    """Only what is left of today's window (07:00-13:00, falls over time)."""

    PAST = "past"
    """Today's window replayed in full after it closed (13:00-16:00)."""


@dataclass(frozen=True)
class T1SocReserve:
    """Reserve for the T1 window of `day`, plus what it was made of.

    `source` names the forecast variant the value came from (`live`,
    `tomorrow_live`, or an EXTRAP key) — surfaced in telegrams and on the
    dashboard so a stale or fallback value is visible.

    `value` includes `margin_pp`; `value_without_margin` is the reading alone.
    `caps` holds the readings of the variants that cap a step (`_MARGIN_STEPS`),
    keyed by `PvForecast.key`, same path as the reading (p60, window, buffer).
    """

    # (until, pp, variants capping the step): the margin in force before
    # `until` on the reserve's day. A capped step is at most what the highest
    # of those variants reads above Live Soft. Keys = `PvForecast.key` (a test
    # keeps them in step with the enum; this module stays free of pv_forecast).
    _MARGIN_STEPS: ClassVar[tuple[tuple[time, int, tuple[str, ...]], ...]] = (
        (time(7), 15, ()),
        (
            time(8),
            10,
            ("live", "extrapolated_live_proportional", "extrapolated_live_blend"),
        ),
        (time(9), 5, ("live",)),
    )

    day: date
    value: int
    flat: int | None
    prev_without_worst: int | None
    dip_kwh: float | None
    source: str
    window: ReserveWindow
    p60: int | None = None
    margin_pp: int = 0
    value_without_margin: int | None = None
    # (PvForecast.key, reading) — a tuple, the VO stays hashable.
    caps: tuple[tuple[str, int | None], ...] = ()

    @classmethod
    def from_target_soc(
        cls,
        day: date,
        target_soc: TargetSoc,
        *,
        source: str,
        window: ReserveWindow,
        margin_at: datetime | None = None,
        caps: Mapping[str, TargetSoc | None] | None = None,
    ) -> T1SocReserve | None:
        """Resolve the reserve from one variant; None when it has nothing yet.

        With `margin_at` the margin in force then goes on top of the reading
        (`margin_pp_for`, capped by the `caps` variants' readings); without it
        there is none (observation-only reserves).
        """
        flat = target_soc.flat.value if target_soc.flat is not None else None
        prev = cls.prev_without_worst_of(
            r.value for r in target_soc.prev_days if r is not None
        )
        driving = cls._reading_of(target_soc)
        if driving is None:
            return None
        cap_readings: dict[str, int | None] = {}
        for key, cap in (caps or {}).items():
            cap_result = cls._reading_of(cap) if cap is not None else None
            cap_readings[key] = cap_result.value if cap_result is not None else None
        margin_pp = (
            cls.margin_pp_for(day, margin_at, driving.value, cap_readings)
            if margin_at is not None
            else 0
        )
        return cls(
            day=day,
            value=min(100, driving.value + margin_pp),
            margin_pp=margin_pp,
            value_without_margin=min(100, driving.value),
            caps=tuple(cap_readings.items()),
            flat=flat,
            prev_without_worst=prev,
            p60=target_soc.p60.value if target_soc.p60 is not None else None,
            dip_kwh=driving.dip_kwh,
            source=source,
            window=window,
        )

    @staticmethod
    def _reading_of(target_soc: TargetSoc) -> TargetSocResult | None:
        """Return the reading the reserve takes: p60 consumption, else flat."""
        return target_soc.p60 if target_soc.p60 is not None else target_soc.flat

    @classmethod
    def margin_pp_for(
        cls,
        day: date,
        moment: datetime,
        reading: int,
        caps: Mapping[str, int | None],
    ) -> int:
        """Margin on top of `reading` (Live Soft) for `day`, seen at `moment`.

        `caps`: readings by `PvForecast.key`; a missing key or None (an Extrap
        before the PV ramps up) does not take part. In a capped step the margin
        is at most the highest present cap above `reading`, never negative;
        with no cap present the step stands whole. The scoreboard calls this
        too, 1:1.
        """
        pp, keys = cls._step_at(day, moment)
        present = [v for k in keys if (v := caps.get(k)) is not None]
        if not present:
            return pp
        return max(0, min(pp, max(present) - reading))

    @classmethod
    def margin_pp_at(cls, day: date, moment: datetime) -> int:
        """Uncapped margin step for the reserve of `day`, seen at `moment`."""
        return cls._step_at(day, moment)[0]

    @classmethod
    def _step_at(cls, day: date, moment: datetime) -> tuple[int, tuple[str, ...]]:
        """(pp, capping variants) in force at `moment` (local time).

        Before that day (the evening preview, the night) the first step holds.
        """
        if moment.date() < day:
            _until, pp, keys = cls._MARGIN_STEPS[0]
            return pp, keys
        if moment.date() > day:
            return 0, ()
        return next(
            (
                (pp, keys)
                for until, pp, keys in cls._MARGIN_STEPS
                if moment.time() < until
            ),
            (0, ()),
        )

    def value_at(self, moment: datetime) -> int:
        """Return the reserve with the margin in force at `moment`, not now's.

        The morning discharge reads its hour's line at the hour's start: the
        plan for 08:00, made at 07:41, must already stand on the 08:00 step.
        """
        base = (
            self.value
            if self.value_without_margin is None
            else self.value_without_margin
        )
        return min(
            100, base + self.margin_pp_for(self.day, moment, base, dict(self.caps))
        )

    @staticmethod
    def prev_without_worst_of(values: Iterable[int]) -> int | None:
        """Highest prev-workday value once the single highest is dropped.

        With one value there is nothing to drop — that value is the answer.
        """
        ordered = sorted(values, reverse=True)
        if not ordered:
            return None
        return ordered[1] if len(ordered) > 1 else ordered[0]

    def same_slot_as(self, other: T1SocReserve | None) -> bool:
        """Tell whether `other` covers the same day, variant and window."""
        return (
            other is not None
            and other.day == self.day
            and other.source == self.source
            and other.window == self.window
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "value": self.value,
            "margin_pp": self.margin_pp,
            "value_without_margin": self.value_without_margin,
            # Flat names for the dashboard; `caps` keeps the keys for restore.
            "live_reading": self._cap("live", "tomorrow_live"),
            "extrap_proportional_reading": self._cap("extrapolated_live_proportional"),
            "extrap_blend_reading": self._cap("extrapolated_live_blend"),
            "caps": dict(self.caps),
            "flat": self.flat,
            "prev_without_worst": self.prev_without_worst,
            "p60": self.p60,
            "dip_kwh": self.dip_kwh,
            "source": self.source,
            "window": self.window.value,
        }

    def _cap(self, *keys: str) -> int | None:
        """First reading held for any of `keys` (today's or tomorrow's variant)."""
        held = dict(self.caps)
        return next((held[k] for k in keys if held.get(k) is not None), None)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> T1SocReserve | None:
        try:
            return cls(
                day=date.fromisoformat(data["day"]),
                value=int(data["value"]),
                flat=data.get("flat"),
                prev_without_worst=data.get("prev_without_worst"),
                p60=data.get("p60"),
                margin_pp=int(data.get("margin_pp", 0)),
                value_without_margin=data.get("value_without_margin"),
                caps=tuple((data.get("caps") or {}).items()),
                dip_kwh=data.get("dip_kwh"),
                source=str(data.get("source", "")),
                window=ReserveWindow(data.get("window", ReserveWindow.FULL.value)),
            )
        except (KeyError, TypeError, ValueError):
            return None
