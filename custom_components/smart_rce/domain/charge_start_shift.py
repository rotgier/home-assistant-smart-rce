"""Charge-start shift — open the battery to PV earlier when the sun won't fill it.

"Charge start" is the moment from which the battery takes the PV surplus;
before it the surplus is exported. `ChargeSlots` puts it in the cheapest hours
(S0) — exporting the dearer morning hours and storing the cheap midday ones.
That only works when the sun from S0 on still fills the battery. When the
forecast says it won't, the start moves earlier, as late as still enough: every
hour moved forward stores an hour that would have sold dearer, and the
shortfall would otherwise be topped up from the grid by the afternoon charge.

The rule, evaluated every 5 minutes from 07:41 (Solcast refreshes at :10/:40):

- need = energy from the current SoC up to full (charge curve);
- window of a start S = [S, first hour pricier than the hour before S);
- candidate = the latest S <= S0 whose forecast surplus in its window covers
  the need — never before 08:00 nor before the first hour with sun; when even
  that one is not enough, it; with no sun before S0, S0 itself;
- the start moves after 3 consecutive candidates on the same side of it, by
  the least of the three, and never later than S0;
- once the start has struck and SoC has risen by 1 pp, the start is locked —
  the battery is taking PV, the decision is made (the effect is measured, not
  the forecast).

Backtest of this rule: `research/2026-10-11-backtest-poranne-ladowanie/shift6.py`.
Times are minutes of the day throughout; S0 may be a half hour (9:30), a moved
start is a whole hour or "now".
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, time
from enum import Enum
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from .pv_surplus import SurplusProfile

FIRST_EVALUATION: Final[int] = 7 * 60 + 41
EARLIEST_START: Final[int] = 8 * 60
MIN_SUN_KWH_PER_H: Final[float] = 0.2


class ShiftVariant(Enum):
    """Forecast surplus variants evaluated side by side; FLAT is the one applied.

    Value: (key, consumption from the median of earlier days?, PV scale). The
    others are observed for 2–3 weeks before choosing on a larger sample.
    """

    FLAT = ("flat", False, 1.0)
    MEDIAN = ("median", True, 1.0)
    MEDIAN_X12 = ("median_x1_2", True, 1.2)
    FLAT_X12 = ("flat_x1_2", False, 1.2)

    def __init__(self, key: str, median: bool, pv_scale: float) -> None:
        self.key = key
        self.median = median
        self.pv_scale = pv_scale


def candidate_start(
    profile: SurplusProfile,
    hour_prices: Sequence[float],
    now: int,
    need_kwh: float,
    s0: int,
) -> int:
    """Return the start this forecast asks for, seen from `now` (minute of day)."""
    first = _first_sun_hour(profile, now, s0)
    if first is None:
        return s0  # no sun before S0 — nothing to gain by opening earlier
    lowest = max(EARLIEST_START, first * 60)
    if lowest > s0:
        return s0
    for start in _starts_latest_first(s0, lowest):
        begin = max(start, now)  # a start already past means "now"
        end = price_window_end(start, hour_prices) * 60
        if profile.total(begin, end) >= need_kwh:
            return begin
    return max(lowest, now)


def _first_sun_hour(profile: SurplusProfile, now: int, s0: int) -> int | None:
    """First hour from now, before S0, with a real surplus (current hour prorated)."""
    for hour in range(now // 60, (s0 - 1) // 60 + 1):
        begin = max(hour * 60, now)
        part = ((hour + 1) * 60 - begin) / 60
        if profile.total(begin, (hour + 1) * 60) >= MIN_SUN_KWH_PER_H * part:
            return hour
    return None


def _starts_latest_first(s0: int, lowest: int) -> list[int]:
    """S0 itself, then the whole hours below it down to `lowest`."""
    whole = range((s0 - 1) // 60, lowest // 60 - 1, -1)
    return [s0, *(hour * 60 for hour in whole if hour * 60 >= lowest)]


def price_window_end(start: int, hour_prices: Sequence[float]) -> int:
    """First hour pricier than the hour before `start` (for 9:30: than hour 9)."""
    hour = start // 60
    ref = hour if start % 60 else hour - 1
    end = hour + 1
    while end < len(hour_prices) and hour_prices[end] < hour_prices[ref]:
        end += 1
    return end


class ChargeStartShift:
    """Today's forecast-driven charge start: confirmation, cap at S0, lock.

    Plain class (entity with a lifecycle): reconstituted from the persisted
    day, S0, start and lock baseline; the last candidates live in memory only —
    after a restart the start needs at most three evaluations to move again.
    """

    CONFIRMATIONS: Final[int] = 3
    LOCK_RISE_PP: Final[float] = 1.0

    def __init__(
        self,
        *,
        day: date,
        s0: int,
        start: int | None = None,
        struck_start: int | None = None,
        soc_at_start: float | None = None,
        locked: bool = False,
    ) -> None:
        self.day = day
        self._s0 = s0
        self._start = s0 if start is None else min(start, s0)
        # Start for which `soc_at_start` was taken — a start moved later after
        # striking strikes again, with a new baseline.
        self._struck_start = struck_start
        self._soc_at_start = soc_at_start
        self._locked = locked
        self._recent: list[int] = []

    @property
    def s0(self) -> int:
        return self._s0

    @property
    def start(self) -> int:
        return self._start

    @property
    def is_shifted(self) -> bool:
        return self._start != self._s0

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def soc_at_start(self) -> float | None:
        return self._soc_at_start

    @property
    def recent(self) -> tuple[int, ...]:
        return tuple(self._recent)

    def evaluate(self, now: int, candidate: int, soc: float | None) -> bool:
        """Take one evaluation; return True when the start moved.

        `soc` None skips the lock (variants that are only observed).
        """
        if self._locked:
            return False
        if soc is not None and self._lock_if_charging(now, soc):
            return False
        self._recent = [*self._recent, candidate][-ChargeStartShift.CONFIRMATIONS :]
        if len(self._recent) < ChargeStartShift.CONFIRMATIONS:
            return False
        moved = self._confirmed_move(now)
        if moved is None or moved == self._start:
            return False
        self._start = moved
        return True

    def _lock_if_charging(self, now: int, soc: float) -> bool:
        """Lock once SoC has risen since the start struck; True when locked now.

        The baseline is the SoC at the first evaluation at or after the start —
        up to one 5-minute tick late, which only makes the lock more cautious.
        """
        if now < self._start:
            return False
        if self._struck_start != self._start or self._soc_at_start is None:
            self._struck_start = self._start
            self._soc_at_start = soc
            return False
        if soc - self._soc_at_start >= ChargeStartShift.LOCK_RISE_PP:
            self._locked = True
            return True
        return False

    def _confirmed_move(self, now: int) -> int | None:
        """Least move all recent candidates agree on, or None."""
        recent = self._recent
        if now < self._start and all(c < self._start for c in recent):
            return max(recent)
        if all(c > self._start for c in recent):
            return min(*recent, self._s0)
        return None

    def restart(self, s0: int) -> bool:
        """Start over from a new S0 (prices moved, or a recompute was asked for).

        A locked start stays — the battery is already taking PV. Returns True
        when anything changed.
        """
        if self._locked:
            return False
        changed = self._s0 != s0 or self._start != s0 or bool(self._recent)
        self._s0 = s0
        self._start = s0
        self._struck_start = None
        self._soc_at_start = None
        self._recent = []
        return changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "s0": self._s0,
            "start": self._start,
            "struck_start": self._struck_start,
            "soc_at_start": self._soc_at_start,
            "locked": self._locked,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ChargeStartShift | None:
        """Restore from persisted dict; None on missing / unparsable input."""
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                day=date.fromisoformat(data["day"]),
                s0=int(data["s0"]),
                start=int(data["start"]),
                struck_start=_optional(int, data.get("struck_start")),
                soc_at_start=_optional(float, data.get("soc_at_start")),
                locked=bool(data.get("locked", False)),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _optional(kind: type[Any], raw: Any) -> Any:
    return None if raw is None else kind(raw)


def to_minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def to_time(minutes: int) -> time:
    return time(minutes // 60, minutes % 60)
