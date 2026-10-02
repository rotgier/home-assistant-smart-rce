"""EveningPlan — which evening hours to discharge into, and how deep.

Replaces an evening ritual: read the prices, find the expensive hours, check
whether tomorrow morning pays better, then translate all that into slot
windows and targets. The rules below are the user's own, written down.

The plan is recomputed rather than stored: it derives entirely from the day's
prices, so both runs of the day build it from scratch. They differ by one
argument — the late-afternoon run knows tomorrow's morning prices, the
previous evening's run does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import ClassVar, Final

from ..tariff import Zone, evening_peak
from ..tariff.table import latest_rates
from .battery_schedule import (
    Scope,
    SetSlotBehaviorCommand,
    SetSlotEnabledCommand,
    SetSlotEndCommand,
    SetSlotStartCommand,
    SetSlotTargetSocCommand,
    SlotBehavior,
    SlotCommand,
    SlotKind,
)
from .rce import RceDayPrices


class EveningPlan:
    """The evening's discharge decision — at most two windows, or none.

    Owns the rules that used to be applied by hand each evening. An hour
    qualifies for discharging when it clears BOTH bars (gross PLN/MWh):

    - `EXPORT_THRESHOLD` — below it the spread over a night-time T3 purchase
      is too thin to be worth emptying the battery; the charge does more good
      feeding the house overnight.
    - the best price tomorrow morning — the same kWh sells once, so if the
      morning pays better the evening stands down.

    Qualifying hours become windows, which map one-to-one onto the two evening
    slots: first window to EARLY, second to LATE.
    """

    # Gross PLN/MWh below which the charge is worth more to the house.
    EXPORT_THRESHOLD: Final[float] = 750.0

    # Hours scanned for "does tomorrow morning outbid tonight". Wider than
    # `discharge_slots.MORNING_DISCHARGE_*` (5-8) on purpose: that constant
    # picks the hour to discharge INTO before PV starts, whereas here we only
    # ask whether the morning beats the evening — and from autumn to late
    # winter 08:00 still produces next to nothing.
    MORNING_LOOKAHEAD: Final[range] = range(5, 9)

    # Evening hours considered on days with no expensive zone at all.
    NON_WORKDAY_EVENING: Final[range] = range(16, 23)

    # A run this long outlasts a single discharge, so it earns two windows.
    _MIN_HOURS_TO_SPLIT: Final[int] = 3

    # How much of T2 is worth considering on a workday, counted back from its
    # end. Three hours covers a full discharge plus a held-back reserve for the
    # last expensive hour; anything earlier is spent before the peak arrives.
    _WORKDAY_HOURS: Final[int] = 3

    # Hours a single discharge covers off-peak — the battery empties in about
    # 1h48m, so a wider window only lets the strategy miss the peak.
    _OFF_PEAK_HOURS: Final[int] = 2

    # Sets the bar for selling the reserve early rather than holding it.
    # Together with the zone gap it lands near 1090 PLN/MWh, which is about
    # where the deposit-aware arithmetic puts the real break-even — see
    # `_sell_early_beats_holding` for why the formula itself does not derive
    # that number and why the gap between the two costs tenths of a zloty.
    _SELL_EARLY_MARGIN: Final[float] = 1.25

    _SCOPE_TODAY: Final[Scope] = "today"
    _SLOT_ORDER: Final[tuple[SlotKind, ...]] = (
        SlotKind.DISCHARGE_EVENING_EARLY,
        SlotKind.DISCHARGE_EVENING_MID,
        SlotKind.DISCHARGE_EVENING_LATE,
    )

    # Which slots carry a plan of N windows. MID only ever joins the three-rung
    # ladder: with two windows the pair stays EARLY + LATE, exactly as before
    # the ladder existed, so an evening that does not need it looks unchanged.
    _SLOTS_BY_COUNT: Final[dict[int, tuple[SlotKind, ...]]] = {
        0: (),
        1: (SlotKind.DISCHARGE_EVENING_EARLY,),
        2: (SlotKind.DISCHARGE_EVENING_EARLY, SlotKind.DISCHARGE_EVENING_LATE),
        3: (
            SlotKind.DISCHARGE_EVENING_EARLY,
            SlotKind.DISCHARGE_EVENING_MID,
            SlotKind.DISCHARGE_EVENING_LATE,
        ),
    }

    # Windows the price-driven path may produce. The third slot belongs to the
    # ladder alone — handing it a third run would park two windows on the same
    # partial target, where the second has nothing left to do.
    _ADAPTIVE_WINDOWS: Final[int] = 2

    def __init__(
        self,
        day: date,
        prices: RceDayPrices,
        *,
        is_workday: bool,
        windows: tuple[EveningWindow, ...] = (),
        max_morning_price: float = 0.0,
        overruled_by_morning: bool = False,
    ) -> None:
        self._day = day
        self._prices = prices
        self._is_workday = is_workday
        self._windows = windows
        self._max_morning_price = max_morning_price
        self._overruled_by_morning = overruled_by_morning

    @classmethod
    def plans_tomorrow_at(cls, now: datetime, *, is_workday: bool = True) -> bool:
        """Tell whether a run at `now` is planning tomorrow evening.

        The switchover is the end of TODAY's window, not a fixed hour: summer
        T2 ends at 22:00, winter's at 21:00, and a day off runs to 23:00.
        Writing tomorrow's plan any earlier would drop windows into hours still
        open today and start a discharge on the spot.

        The same rule stamps a hand edit, so an evening set by hand and an
        evening planned automatically always mean the same day.
        """
        return now.hour >= cls._evening_hours(now.date(), is_workday).stop

    @classmethod
    def for_day(
        cls,
        day: date,
        prices: RceDayPrices,
        *,
        is_workday: bool,
        tomorrow: RceDayPrices | None = None,
    ) -> EveningPlan:
        """Build the plan for `day`. `tomorrow` absent = morning filter off.

        Before tomorrow's prices are published there is nothing to compare
        against, so the evening is judged on its own merits and the later run
        of the day revisits it.
        """
        max_morning_price = (
            tomorrow.max_gross_in(cls.MORNING_LOOKAHEAD) if tomorrow else 0.0
        )
        windows = cls._windows_for(day, prices, is_workday, max_morning_price)
        unfiltered = cls._windows_for(day, prices, is_workday, 0.0)
        return cls(
            day,
            prices,
            is_workday=is_workday,
            windows=windows,
            max_morning_price=max_morning_price,
            overruled_by_morning=len(windows) < len(unfiltered),
        )

    @property
    def day(self) -> date:
        """Evening this plan is for — today's or tomorrow's, per the run."""
        return self._day

    @property
    def windows(self) -> tuple[EveningWindow, ...]:
        return self._windows

    @property
    def is_empty(self) -> bool:
        return not self._windows

    @property
    def overruled_by_morning(self) -> bool:
        """True when the morning filter removed windows that would otherwise stand.

        The caller uses this to decide whether it may overwrite a hand-set
        schedule: a plan emptied by tomorrow's morning is new information,
        while an ordinary empty plan is not a reason to touch anything.
        """
        return self._overruled_by_morning

    @property
    def max_morning_price(self) -> float:
        """Best gross price tomorrow morning that this plan had to beat; 0 when unknown."""
        return self._max_morning_price

    @property
    def reason(self) -> str:
        """One line for the Telegram summary."""
        if self._windows:
            hours = ", ".join(w.describe() for w in self._windows)
            return f"windows: {hours}"
        if self._max_morning_price > 0:
            return (
                f"no evening hour clears {self.EXPORT_THRESHOLD:.0f} "
                f"and tomorrow morning's {self._max_morning_price:.0f} (gross PLN/MWh)"
            )
        return f"no evening hour clears {self.EXPORT_THRESHOLD:.0f} gross PLN/MWh"

    def slot_commands(self) -> list[SlotCommand]:
        """Commands putting this plan into the two evening slots.

        Slots without a window are disabled rather than left alone — an old
        window surviving into a day that does not want it is exactly the kind
        of stale setting this plan exists to prevent.
        """
        carried = dict(
            zip(self._SLOTS_BY_COUNT[len(self._windows)], self._windows, strict=True)
        )
        commands: list[SlotCommand] = []
        for kind in self._SLOT_ORDER:
            window = carried.get(kind)
            if window is None:
                commands.append(
                    SetSlotEnabledCommand(
                        scope=self._SCOPE_TODAY, kind=kind, value=False
                    )
                )
                continue
            commands.extend(window.commands_for(kind, scope=self._SCOPE_TODAY))
        return commands

    @classmethod
    def _windows_for(
        cls,
        day: date,
        prices: RceDayPrices,
        is_workday: bool,
        max_morning_price: float,
    ) -> tuple[EveningWindow, ...]:
        """Qualifying hours → grouped runs → at most two windows."""
        candidates = prices.hours_clearing(
            cls._evening_hours(day, is_workday),
            at_least=cls.EXPORT_THRESHOLD,
            above=max_morning_price,
        )
        if is_workday:
            runs = cls._contiguous_runs(candidates)
            if ladder := cls._descending_ladder(runs, prices):
                return ladder
            runs = cls._split_long_run(runs, prices, is_workday)
        else:
            runs = cls._best_off_peak_pair(candidates, prices)
        usable = runs[: cls._ADAPTIVE_WINDOWS]
        zone_end = cls._expensive_zone_end(day, is_workday)
        return tuple(
            EveningWindow.covering(
                tuple(hours),
                prices,
                zone_end=zone_end,
                is_last=index == len(usable) - 1,
            )
            for index, hours in enumerate(usable)
        )

    @classmethod
    def _evening_hours(cls, day: date, is_workday: bool) -> range:
        """Hours worth considering on `day`.

        On a workday: the LAST THREE hours of T2, whatever the season. Summer's
        block (19-22) is three hours anyway; winter's (16-21) is five, and its
        first two are no use — the battery empties in under two hours, so
        starting at 16:00 would spend the charge before the expensive evening
        even peaks.

        Off-peak days have no T2, so the whole evening is fair game and the
        best pair inside it gets picked later.
        """
        if not is_workday:
            return cls.NON_WORKDAY_EVENING
        peak = evening_peak(day)
        return range(max(peak.start, peak.stop - cls._WORKDAY_HOURS), peak.stop)

    @classmethod
    def _best_off_peak_pair(
        cls, candidates: list[int], prices: RceDayPrices
    ) -> list[list[int]]:
        """Pick the best adjacent pair of hours — the whole plan on a day off.

        With no expensive zone to reserve charge for, the only question is
        where to sell, and the battery empties in about 1h48m. Two adjacent
        hours is therefore the entire answer: a wider window only lets the
        strategy drift off the peak, and a second window would have nothing
        left to discharge. That is why days off never use the LATE slot.

        Falls back to the single best hour when no two qualify side by side.
        """
        pairs = [
            (first, second)
            for first, second in zip(candidates, candidates[1:], strict=False)
            if second == first + 1
        ]
        if pairs:
            best = max(
                pairs, key=lambda p: prices.gross_at(p[0]) + prices.gross_at(p[1])
            )
            return [list(best)]
        return [[max(candidates, key=prices.gross_at)]]

    @staticmethod
    def _contiguous_runs(hours: list[int]) -> list[list[int]]:
        """Split qualifying hours into runs; a cheap hour between them breaks one."""
        runs: list[list[int]] = []
        for hour in hours:
            if runs and hour == runs[-1][-1] + 1:
                runs[-1].append(hour)
            else:
                runs.append([hour])
        return runs

    @classmethod
    def _descending_ladder(
        cls, runs: list[list[int]], prices: RceDayPrices
    ) -> tuple[EveningWindow, ...] | None:
        """Three one-hour rungs, or None when the evening does not call for them.

        Used on exactly one shape: a single unbroken run of three expensive
        hours whose prices fall from the first to the third. That shape is the
        one the price-driven path handles badly. It picks IMMEDIATE (the first
        hour pays best), empties the battery to the reserve well before the
        second hour closes, and from then until the last hour opens the house
        quietly eats the reserve — so the last expensive hour, which still has
        to be covered, starts short.

        Every other shape is left alone on purpose. With the peak in the middle
        or at the end the same path already picks DELAYED_TO_END and lands on
        the reserve as the hour turns, which is exactly what this ladder has to
        arrange by hand.

        Each rung ends where an hour ends, so holding it back costs nothing:
        hourly settlement nets the whole hour, and within one hour it does not
        matter when the battery gave what it gave.
        """
        if len(runs) != 1 or len(runs[0]) != cls._MIN_HOURS_TO_SPLIT:
            return None
        hours = runs[0]
        paid = [prices.gross_at(hour) for hour in hours]
        if not paid[0] > paid[1] > paid[2]:
            return None
        return tuple(
            EveningWindow.rung(hour, target_soc=target, behavior=behavior)
            for hour, (target, behavior) in zip(
                hours, EveningWindow.LADDER, strict=True
            )
        )

    @classmethod
    def _split_long_run(
        cls, runs: list[list[int]], prices: RceDayPrices, is_workday: bool
    ) -> list[list[int]]:
        """Break a lone run of three-plus hours into a head and a final hour.

        Emptying a full battery takes roughly 1h48m, so one window spanning
        three expensive hours would reach only two of them — starting at once
        would skip the last, waiting would skip the first. Two windows, with
        the head stopping at the partial target, put energy into all three.

        Only an undivided run is split; once prices produced two runs there
        are no slots left to give.
        """
        if len(runs) != 1 or len(runs[0]) < cls._MIN_HOURS_TO_SPLIT:
            return runs
        run = runs[0]
        if cls._sell_early_beats_holding(run, prices, is_workday):
            return [run]
        return [run[:-1], run[-1:]]

    @classmethod
    def _sell_early_beats_holding(
        cls, run: list[int], prices: RceDayPrices, is_workday: bool
    ) -> bool:
        """Tell whether emptying into the earlier hours beats keeping a reserve.

        Holding back covers the house through the last expensive hour instead
        of buying it at the T2 rate. Selling the reserve an hour early earns
        the price difference instead, so it wins only when that difference
        beats what the house then buys back.

        The threshold is a stand-in, not a derivation, and the zone gap it is
        built from does not belong in the arithmetic: the battery ends the
        evening at 10% either way, so the night top-up — and with it the T3
        price — is identical on both paths and cancels. What the bar should
        answer to is the deposit: with the balance in surplus an exported kWh
        is worth about 30% of its RCE, while the distribution half of a T2
        purchase is paid in cash whatever the deposit does. Working that
        through puts the honest threshold at roughly 1100-1500 PLN/MWh, which
        is where this constant happens to sit. It is kept because it lands in
        the right place, not because the formula explains it.

        What makes the imprecision affordable is the domain. Every candidate
        hour already clears `EXPORT_THRESHOLD`, so the last one is never below
        750 gross and this can only fire above roughly 1840. On top of that a
        trio whose prices fall all the way is claimed by the ladder, and
        `_WORKDAY_HOURS` caps a run at three — so what is left is a middle
        hour paying over 1840 with a cheap hour behind it. A handful of hours
        a year, worth tenths of a zloty when the call goes the wrong way.

        Off-peak days have no T2 to protect against, so the question does not
        arise; the last window already empties the battery there.
        """
        if not is_workday or len(run) < 2:
            return False
        rates = latest_rates()
        zone_gap = rates.marginal_cost(Zone.T2) - rates.marginal_cost(Zone.T3)
        price_gap = prices.gross_at(run[-2]) - prices.gross_at(run[-1])
        return price_gap > zone_gap * cls._SELL_EARLY_MARGIN

    @staticmethod
    def _expensive_zone_end(day: date, is_workday: bool) -> int | None:
        """Hour at which T2 ends, or None on days that have no expensive zone."""
        if not is_workday:
            return None
        return evening_peak(day).stop


@dataclass(frozen=True)
class EveningWindow:
    """One contiguous stretch of hours to discharge into — maps onto one slot.

    Value object: `target_soc` and `behavior` follow from the hours and the
    surrounding day, and are settled once by `covering` rather than by whoever
    happens to build the window.
    """

    # Left in the battery when an expensive hour still lies ahead. 33 pp above
    # the floor is roughly what the house draws over one evening hour, measured
    # at 1.17 kWh on 02.10 against 1.58 kWh for the 33->10% stretch.
    PARTIAL_TARGET_SOC: ClassVar[float] = 33.0
    FULL_TARGET_SOC: ClassVar[float] = 10.0

    # Top rung of the ladder: one hour of discharge below a nearly full
    # battery. At 75 s/pp above 25% an hour sheds about 48 pp, so a battery
    # starting near 95% lands here as the hour closes; starting from 100% the
    # rung simply runs out of hour and stops a few points high, which the next
    # rung absorbs because its target is absolute.
    FIRST_RUNG_TARGET_SOC: ClassVar[float] = 47.0

    # Targets and behaviors of the three rungs, in slot order. The two upper
    # rungs have to be standing on their target when the hour turns, which is
    # what DELAYED_TO_END arranges; the last one empties and may as well do it
    # at once.
    LADDER: ClassVar[tuple[tuple[float, SlotBehavior], ...]] = (
        (FIRST_RUNG_TARGET_SOC, SlotBehavior.DELAYED_TO_END),
        (PARTIAL_TARGET_SOC, SlotBehavior.DELAYED_TO_END),
        (FULL_TARGET_SOC, SlotBehavior.IMMEDIATE),
    )

    hours: tuple[int, ...]
    target_soc: float
    behavior: SlotBehavior

    @classmethod
    def covering(
        cls,
        hours: tuple[int, ...],
        prices: RceDayPrices,
        *,
        zone_end: int | None,
        is_last: bool,
    ) -> EveningWindow:
        """Build the window over `hours`, settling how deep and when to run."""
        return cls(
            hours=hours,
            target_soc=cls._target_for(hours, zone_end, is_last=is_last),
            behavior=cls._behavior_for(hours, prices),
        )

    @classmethod
    def rung(
        cls, hour: int, *, target_soc: float, behavior: SlotBehavior
    ) -> EveningWindow:
        """One step of the ladder — target and behavior come from it, not from prices."""
        return cls(hours=(hour,), target_soc=target_soc, behavior=behavior)

    @property
    def start(self) -> time:
        return time(self.hours[0], 0)

    @property
    def end(self) -> time:
        """Exclusive end — the hour after the last one covered."""
        return time(self.hours[-1] + 1, 0)

    @property
    def is_single_hour(self) -> bool:
        return len(self.hours) < 2

    def reaches(self, hour: int) -> bool:
        """Tell whether the window runs up to (or past) `hour`."""
        return self.hours[-1] + 1 >= hour

    def describe(self) -> str:
        """Compact form for the Telegram summary, e.g. `19-21 to 33%`."""
        return (
            f"{self.start:%H:%M}-{self.end:%H:%M} to {self.target_soc:.0f}% "
            f"({self.behavior.value.lower()})"
        )

    def commands_for(self, kind: SlotKind, *, scope: Scope) -> list[SlotCommand]:
        """Express this window in the language the schedule aggregate speaks."""
        return [
            SetSlotEnabledCommand(scope=scope, kind=kind, value=True),
            SetSlotStartCommand(scope=scope, kind=kind, value=self.start),
            SetSlotEndCommand(scope=scope, kind=kind, value=self.end),
            SetSlotTargetSocCommand(scope=scope, kind=kind, value=self.target_soc),
            SetSlotBehaviorCommand(scope=scope, kind=kind, value=self.behavior),
        ]

    @classmethod
    def _target_for(
        cls, hours: tuple[int, ...], zone_end: int | None, *, is_last: bool
    ) -> float:
        """Hold charge back only while an expensive hour still lies ahead.

        Holding back pays when the window ends before T2 does, because the
        house would otherwise buy those hours at the T2 rate. Three cases
        empty the battery instead: a day with no expensive zone at all, a
        window running to the end of T2, and a single-hour window — at full
        charge the target is out of reach anyway (~50 pp/h against 90 pp to
        give), and from a low start we would rather sell everything into that
        one expensive hour than ration it.

        An earlier window always holds back; the later one finishes the job.
        """
        if not is_last:
            return cls.PARTIAL_TARGET_SOC
        if zone_end is None or len(hours) < 2 or hours[-1] + 1 >= zone_end:
            return cls.FULL_TARGET_SOC
        return cls.PARTIAL_TARGET_SOC

    @staticmethod
    def _behavior_for(hours: tuple[int, ...], prices: RceDayPrices) -> SlotBehavior:
        """Push the discharge toward whichever end of the window pays more."""
        if len(hours) < 2:
            return SlotBehavior.IMMEDIATE
        if prices.gross_at(hours[0]) >= prices.gross_at(hours[-1]):
            return SlotBehavior.IMMEDIATE
        return SlotBehavior.DELAYED_TO_END
