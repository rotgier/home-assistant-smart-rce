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
  the need — never before 08:00 nor before the first hour with sun; when no
  start is enough (the sun cannot fill the battery anyway), 08:00 — or now;
- the start moves after 3 consecutive candidates on the same side of it, by
  the least of the three, and never later than S0;
- once the start has struck and SoC has risen by 1 pp, the start is locked —
  the battery is taking PV, the decision is made (the effect is measured, not
  the forecast).

Applied variant (GUARDED, chosen after `shift7.py`): a move EARLIER is trusted
to Live alone only when even the earliest start is not enough; otherwise it
waits for 09:10 (fresh Solcast, first reliable Extrap) and needs Live and
Extrap Proportional to agree — both earlier, the later of the two. Live
under-reads sunny mornings 2-3x; waiting cut the early moves to zero at the
cost of one start an hour late. Moves later follow Live as before.
Since 14.10.2026 the PV behind "Live" here is Live Soft (Solcast median +
0.4 of Live's weather adjustment) and, since 15.10.2026, the consumption is the
per-hour p60 of earlier days of the same type instead of a flat 0.9 kWh/h
(replay 04-14.10: flat started 3-5x too late, the median once, by 0.2-0.3 kWh;
at 08:41 over 7 days p60 was on time 4x and never late, the median late once).
The guard was backtested on plain Live with flat consumption.

Backtest of this rule: `research/2026-10-11-backtest-poranne-ladowanie/shift6.py`.
Times are minutes of the day throughout; S0 may be a half hour (9:30), a moved
start is a whole hour or "now".
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, time
from enum import Enum
from typing import TYPE_CHECKING, Any, Final

from .battery_schedule.direction import Direction
from .charge_curve import FULL_SOC, kwh_per_pp, kwh_to_full, pp_below_full
from .pv_surplus import SLOT_MINUTES

if TYPE_CHECKING:
    from .pv_surplus import SurplusProfile

FIRST_EVALUATION: Final[int] = 7 * 60 + 41
EARLIEST_START: Final[int] = 8 * 60
MIN_SUN_KWH_PER_H: Final[float] = 0.2
EXTRAP_FROM: Final[int] = 9 * 60 + 10
REWIND_FROM: Final[int] = 9 * 60
# The BMS holds SoC here for ~10 minutes before the fast top (17.10.2026:
# 90% from 12:56 to 13:06); the chart splits the PV charge at it.
PLATEAU_SOC: Final[float] = 90.0
# A start later than the base one (`cheaper_later_start`): the surplus from it
# covers the need this many times over, and the battery is full this many
# minutes before T2 (user's decision 28.10.2026).
LATER_MARGIN: Final[float] = 1.5
LATER_FULL_BEFORE_END: Final[int] = 60
# ...and its energy is at least this much cheaper (zl/MWh net, mean of what the
# battery takes) — near-equal hours must not toss the start back and forth.
LATER_MIN_GAIN: Final[float] = 10.0


class ShiftVariant(Enum):
    """Forecast variants evaluated side by side; GUARDED is the one applied.

    Value: (key, consumption from the p60 of earlier days?, PV scale,
    guarded by Extrap?). Consumption is flat 0.9 kWh/h unless history. The
    observed ones are kept for 2–3 weeks before choosing on a larger sample.
    """

    GUARDED = ("guarded", True, 1.0, True)
    FLAT = ("flat", False, 1.0, False)
    P60 = ("p60", True, 1.0, False)
    P60_X12 = ("p60_x1_2", True, 1.2, False)
    FLAT_X12 = ("flat_x1_2", False, 1.2, False)

    def __init__(self, key: str, history: bool, pv_scale: float, guarded: bool) -> None:
        self.key = key
        self.history = history
        self.pv_scale = pv_scale
        self.guarded = guarded


APPLIED_VARIANT: Final = ShiftVariant.GUARDED


@dataclass(frozen=True)
class ChargeMilestones:
    """Minutes of the day a charge reaches the plateau and full; None = not by the end.

    `end_soc` is the level at the end of the window (100 when full before it).
    """

    plateau_at: int | None
    full_at: int | None
    end_soc: float


@dataclass(frozen=True)
class Candidate:
    """A forecast's start; `enough` False when even the earliest start falls short."""

    start: int
    enough: bool


def candidate_start(
    profile: SurplusProfile,
    hour_prices: Sequence[float],
    now: int,
    need_kwh: float,
    s0: int,
) -> int:
    """Return the start this forecast asks for, seen from `now` (minute of day)."""
    return find_candidate(profile, hour_prices, now, need_kwh, s0).start


def find_candidate(
    profile: SurplusProfile,
    hour_prices: Sequence[float],
    now: int,
    need_kwh: float,
    s0: int,
) -> Candidate:
    """Like `candidate_start`, telling apart "enough" from "earliest, still short"."""
    return find_candidate_at(profile, hour_prices, now, s0, lambda _start: need_kwh)


def find_candidate_at(
    profile: SurplusProfile,
    hour_prices: Sequence[float],
    now: int,
    s0: int,
    need_at: Callable[[int], float],
) -> Candidate:
    """Like `find_candidate`, the need taken at each start tried.

    `need_at(start)`: energy to full from the level the battery is expected at
    when that start strikes — the PV charge outlook starts from the T1 reserve
    with the margin of the start's hour (more at 08:00 than later).
    """
    first = _first_sun_hour(profile, now, s0)
    # No sun before S0: only S0 itself is worth checking.
    lowest = s0 if first is None else min(max(EARLIEST_START, first * 60), s0)
    for start in _starts_latest_first(s0, lowest):
        begin = max(start, now)  # a start already past means "now"
        end = price_window_end(start, hour_prices) * 60
        if profile.total(begin, end) >= need_at(begin):
            return Candidate(begin, True)
    # Short even at the earliest start: open from 08:00 regardless of the sun —
    # every kWh caught from PV saves a grid top-up (~0.73 zl vs ~0.15 exported),
    # and while the house uses it all an open battery simply takes nothing.
    return Candidate(max(min(EARLIEST_START, s0), now), False)


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


def cheaper_later_start(
    profile: SurplusProfile,
    hour_prices: Sequence[float],
    now: int,
    base: Candidate,
    need_at: Callable[[int], float],
    *,
    window_end: int,
    held: int | None = None,
) -> Candidate:
    """Return `base`, or a later whole-hour start where the battery takes cheaper PV.

    A battery that needs little is filled within an hour or two, so the start
    can wait for the cheapest hours and export the dearer ones before them —
    later than S0 too. Only when `base` itself fills the battery (`enough`);
    a later start has to keep a margin inside its price window — the hours
    cheaper than the one before it (`price_window_end`, the rule the earlier
    moves count by: charging on into a dearer hour is worse than having
    started an hour earlier, user 28.10.2026), and no later than an hour
    before `window_end` (T2): the surplus there is at least `LATER_MARGIN` x
    the need, and the charge model fills the battery there. The price of a start is the mean
    price of the energy the battery actually takes from it (minute by minute,
    until full), not of the hour it starts in; a later start wins only when
    `LATER_MIN_GAIN` cheaper than `base`, the cheapest one (the earliest among
    equals).

    `held`: the later start already in force. It stays while it keeps the
    margin and costs no more than `base` — the bar is for moving, not for
    staying: past S0 `base` is "now", and close to the held hour starting now
    takes almost nothing at the dearer price, so the gap shrinks under the bar
    (28.10.2026: 10:00 dropped to 09:41 on 834 vs 831). Another later hour
    replaces it only when `LATER_MIN_GAIN` cheaper than it.

    `need_at(start)`: energy to full at that start, the drain until then
    included — the caller knows the level the battery is left at.
    """
    if not base.enough:
        return base
    deadline = window_end - LATER_FULL_BEFORE_END
    base_price = _taken_price(profile, hour_prices, base.start, need_at)
    if base_price is None:
        return base
    options: list[tuple[int, float]] = []
    first_hour = base.start // 60 + 1
    for hour in range(max(first_hour, -(-now // 60)), deadline // 60):
        start = hour * 60
        need = need_at(start)
        end = min(price_window_end(start, hour_prices) * 60, deadline)
        if profile.total(start, end) < LATER_MARGIN * need:
            continue
        soc = FULL_SOC - pp_below_full(need)
        if charge_milestones(profile, start, soc, end).full_at is None:
            continue
        price = _taken_price(profile, hour_prices, start, need_at)
        bar = base_price if start == held else base_price - LATER_MIN_GAIN
        if price is not None and price <= bar:
            options.append((start, price))
    if not options:
        return base
    start, price = min(options, key=lambda option: (option[1], option[0]))
    kept = next((o for o in options if o[0] == held), None)
    if kept is not None and price > kept[1] - LATER_MIN_GAIN:
        start = kept[0]
    return Candidate(start, True)


def _taken_price(
    profile: SurplusProfile,
    hour_prices: Sequence[float],
    start: int,
    need_at: Callable[[int], float],
) -> float | None:
    """Mean price of the energy the battery takes charging from `start` until full."""
    level = FULL_SOC - pp_below_full(need_at(start))
    cost = taken = 0.0
    for minute in range(start, len(hour_prices) * 60):
        if level >= FULL_SOC:
            break
        slot = profile.slots[minute // SLOT_MINUTES]
        surplus_pp = slot.surplus_kwh / SLOT_MINUTES / kwh_per_pp(level)
        step = min(surplus_pp, 60 / _charge_sec_per_pp(level), FULL_SOC - level)
        kwh = step * kwh_per_pp(level)
        cost += kwh * hour_prices[minute // 60]
        taken += kwh
        level += step
    return cost / taken if taken > 0 else None


def lowest_soc_the_sun_fills(
    profile: SurplusProfile,
    hour_prices: Sequence[float],
    now: int,
    s0: int,
    floor: int,
) -> int:
    """Lowest SoC >= `floor` from which this forecast's start still fills the battery.

    The automatic morning discharge stands on it: below, the battery would end
    the day short and the afternoon charge would buy the rest from the grid.
    100 when not even an almost full battery gets topped up.
    """
    for soc in range(floor, 100):
        if find_candidate(profile, hour_prices, now, kwh_to_full(soc), s0).enough:
            return soc
    return 100


def full_charge_minute(
    profile: SurplusProfile,
    start: int,
    soc: float,
    end: int,
    full_soc: float = FULL_SOC,
) -> int | None:
    """Minute of the day the battery is full charging from `start`; None if not by `end`."""
    return charge_milestones(profile, start, soc, end, full_soc).full_at


def charge_milestones(
    profile: SurplusProfile,
    start: int,
    soc: float,
    end: int,
    full_soc: float = FULL_SOC,
) -> ChargeMilestones:
    """When the battery reaches the plateau level and full charging from `start`.

    Minute by minute: the surplus of the slot (spread evenly over its 30
    minutes) goes in, at most at the charge pace of `Direction.CHARGE` (the
    same SoC-zone model the charge slots run on: ~75 s/pp up to 89%, then the
    BMS plateau and the slow top). Energy per pp from the charge curve.
    `full_soc`: the level that counts as full — 100 for decisions; the
    scoreboard compares the model against the BMS reading of 99%.
    """
    level = soc
    plateau_at: int | None = start if soc >= PLATEAU_SOC else None
    for minute in range(start, end):
        if level >= full_soc:
            return ChargeMilestones(plateau_at, minute, level)
        slot = profile.slots[minute // SLOT_MINUTES]
        surplus_pp = slot.surplus_kwh / SLOT_MINUTES / kwh_per_pp(level)
        pace_pp = 60 / _charge_sec_per_pp(level)
        level = min(FULL_SOC, level + min(surplus_pp, pace_pp))
        if plateau_at is None and level >= PLATEAU_SOC:
            plateau_at = minute + 1
    full_at = end if level >= full_soc else None
    return ChargeMilestones(plateau_at, full_at, level)


def _charge_sec_per_pp(soc: float) -> float:
    return next(
        z.sec_per_pp
        for z in Direction.CHARGE.rate_zones
        if z.soc_from <= soc < z.soc_to
    )


def guarded_candidate(
    live: Candidate, extrap: int | None, current: int, now: int
) -> int:
    """Live's candidate, held back from moving earlier until Extrap agrees.

    Returns `current` (no signal — breaks a run of confirmations) when an
    earlier move is not trusted yet.
    """
    if live.start >= current:
        return live.start
    if now < EXTRAP_FROM:
        return current if live.enough else live.start
    if extrap is not None and extrap < current:
        return max(live.start, extrap)
    return current


def price_window_end(start: int, hour_prices: Sequence[float]) -> int:
    """First hour pricier than the hour before `start` (for 9:30: than hour 9)."""
    hour = start // 60
    ref = hour if start % 60 else hour - 1
    end = hour + 1
    while end < len(hour_prices) and hour_prices[end] < hour_prices[ref]:
        end += 1
    return end


class ChargeStartShift:
    """Today's forecast-driven charge start: confirmation, lock.

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
        # Later than S0 too since 28.10.2026 (`cheaper_later_start`).
        self._start = s0 if start is None else start
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
        candidate = self._as_move(now, candidate)
        self._recent = [*self._recent, candidate][-ChargeStartShift.CONFIRMATIONS :]
        if len(self._recent) < ChargeStartShift.CONFIRMATIONS:
            return False
        moved = self._confirmed_move(now)
        if moved is None or moved == self._start:
            return False
        self._start = moved
        return True

    def _as_move(self, now: int, candidate: int) -> int:
        """Read a candidate of "now" after the start has struck as no move.

        `find_candidate` answers "now" for a start already past (and for a
        forecast short at any start). Once the start has struck the battery is
        already open, so "open from now" asks for nothing new — taking it as
        "later" moved the start to now - 10 min every 5 minutes (14.10.2026),
        each move re-taking the lock baseline, so the lock never came.
        """
        if now >= self._start and candidate <= now:
            return self._start
        return candidate

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
            return min(recent)
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


class RewindWatch:
    """Watch a locked start for a later one that would still fill the battery.

    Once charging has started the shift is locked and never moved back on its
    own. This only reports — with a phone call — that pausing until a later
    start looks worth it, and the user decides (a hand-set start wins). Due
    when, from 09:00, Live and Extrap Proportional agree three times in a row
    that a start at least 30 minutes from now still fills the battery from the
    current SoC; at most three alerts a day. In memory only: deliberately rough,
    meant to get a feel for how often it would fire.
    """

    CONFIRMATIONS: Final[int] = 3
    MIN_GAIN_MINUTES: Final[int] = 30
    MAX_ALERTS_PER_DAY: Final[int] = 3

    def __init__(self, day: date) -> None:
        self.day = day
        self.alerts = 0
        self.last_suggestion: int | None = None
        self._recent: list[int | None] = []

    @property
    def recent(self) -> tuple[int | None, ...]:
        return tuple(self._recent)

    def evaluate(
        self, now: int, live: Candidate, extrap: Candidate | None
    ) -> int | None:
        """Take one evaluation; return the later start to suggest, when due."""
        if self.alerts >= RewindWatch.MAX_ALERTS_PER_DAY:
            return None
        self._recent = [*self._recent, self._agreed_later(now, live, extrap)][
            -RewindWatch.CONFIRMATIONS :
        ]
        recent = self._recent
        if len(recent) < RewindWatch.CONFIRMATIONS or None in recent:
            return None
        suggestion = min(c for c in recent if c is not None)
        self._recent = []
        self.alerts += 1
        self.last_suggestion = suggestion
        return suggestion

    @staticmethod
    def _agreed_later(
        now: int, live: Candidate, extrap: Candidate | None
    ) -> int | None:
        """Return the earlier of the two later starts, when both fill the battery."""
        if extrap is None or not (live.enough and extrap.enough):
            return None
        later = min(live.start, extrap.start)
        return later if later - now >= RewindWatch.MIN_GAIN_MINUTES else None


def _optional(kind: type[Any], raw: Any) -> Any:
    return None if raw is None else kind(raw)


def to_minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def to_time(minutes: int) -> time:
    return time(minutes // 60, minutes % 60)


def to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
