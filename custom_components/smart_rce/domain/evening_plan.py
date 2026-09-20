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
from .tariff import G13Zone

# From this hour a planning run targets tomorrow evening: today's windows are
# behind us, and writing them again would re-open a window already past.
_PLANNING_LOOKS_AHEAD_FROM_HOUR: Final[int] = 22


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

    _SCOPE_TODAY: Final[Scope] = "today"
    _SLOT_ORDER: Final[tuple[SlotKind, ...]] = (
        SlotKind.DISCHARGE_EVENING_EARLY,
        SlotKind.DISCHARGE_EVENING_LATE,
    )

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

    @staticmethod
    def plans_tomorrow_at(now: datetime) -> bool:
        """Tell whether a run at `now` is planning tomorrow evening.

        Tonight's windows have passed by this hour, so a later run looks ahead
        instead. Writing tomorrow's plan before that point would drop windows
        into hours still open today and start a discharge on the spot.
        """
        return now.hour >= _PLANNING_LOOKS_AHEAD_FROM_HOUR

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
        commands: list[SlotCommand] = []
        for kind, window in zip(self._SLOT_ORDER, self._padded_windows(), strict=True):
            if window is None:
                commands.append(
                    SetSlotEnabledCommand(
                        scope=self._SCOPE_TODAY, kind=kind, value=False
                    )
                )
                continue
            commands.extend(window.commands_for(kind, scope=self._SCOPE_TODAY))
        return commands

    def _padded_windows(self) -> tuple[EveningWindow | None, ...]:
        """Windows aligned to the slot order, padded with None."""
        missing = len(self._SLOT_ORDER) - len(self._windows)
        return self._windows + (None,) * missing

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
        runs = cls._split_long_run(cls._contiguous_runs(candidates))
        usable = runs[: len(cls._SLOT_ORDER)]
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
        """Hours worth considering — the T2 block, or a broad evening off-peak."""
        if not is_workday:
            return cls.NON_WORKDAY_EVENING
        start, end = G13Zone.evening_peak_hours(day)
        return range(start, end)

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
    def _split_long_run(cls, runs: list[list[int]]) -> list[list[int]]:
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
        return [run[:-1], run[-1:]]

    @staticmethod
    def _expensive_zone_end(day: date, is_workday: bool) -> int | None:
        """Hour at which T2 ends, or None on days that have no expensive zone."""
        if not is_workday:
            return None
        return G13Zone.evening_peak_hours(day)[1]


@dataclass(frozen=True)
class EveningWindow:
    """One contiguous stretch of hours to discharge into — maps onto one slot.

    Value object: `target_soc` and `behavior` follow from the hours and the
    surrounding day, and are settled once by `covering` rather than by whoever
    happens to build the window.
    """

    # Left in the battery when an expensive hour still lies ahead.
    PARTIAL_TARGET_SOC: ClassVar[float] = 33.0
    FULL_TARGET_SOC: ClassVar[float] = 10.0

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
