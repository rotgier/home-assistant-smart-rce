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

from ..tariff import evening_peak
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
    """The evening's discharge decision — one rung per selling hour, or none.

    Owns the rules that used to be applied by hand each evening. On a workday
    every T2 hour is judged on its own (user's decision 23.10.2026) and sells
    when it clears all three (gross PLN/MWh):

    - `EXPORT_THRESHOLD` — below it the spread over a night-time T3 purchase
      is too thin to be worth emptying the battery; the charge does more good
      feeding the house overnight;
    - the best price tomorrow morning — the same kWh sells once, so if the
      morning pays better the evening holds it;
    - every later evening hour, up to the first hour after T2 — the same kWh
      sells for more an hour later (21.10.2026: 20:00 at 759 held back for
      21:00 at 819). A tie sells in the earlier hour.

    A selling hour discharges down to the reserve the T2 hours still ahead of
    it need, one rung per hour (`_rung_for`): winter 16:00 75%, 17:00 61%,
    18:00 47%, 19:00 33%, 20:00 10%. A day off has no T2: the best pair of
    hours from 16:00 empties the battery in one window.
    """

    # Gross PLN/MWh below which the charge is worth more to the house.
    EXPORT_THRESHOLD: Final[float] = 750.0

    # Hours scanned for "does tomorrow morning outbid tonight" — from autumn
    # to late winter 08:00 still produces next to nothing.
    MORNING_LOOKAHEAD: Final[range] = range(5, 9)

    # Evening hours considered on days with no expensive zone at all.
    NON_WORKDAY_EVENING: Final[range] = range(16, 23)

    # Workday rungs by the T2 hours left after the hour (0 = the last one):
    # (slot, SoC to stand on when the hour closes). The last hour empties to
    # the floor; 33% is one evening hour of the house with a margin (02.10.2026:
    # 1.17 kWh drawn against 1.58 kWh for 33->10%); each earlier rung holds
    # ~14 pp more, about one hour of discharge (75 s/pp above 25% sheds ~48 pp
    # an hour, so a nearly full battery reaches 47% within its hour). Winter's
    # T2 is five hours (16-21), summer's three (19-22, from EARLY on).
    _RUNGS: Final[tuple[tuple[SlotKind, float], ...]] = (
        (SlotKind.DISCHARGE_EVENING_LATE, 10.0),
        (SlotKind.DISCHARGE_EVENING_MID, 33.0),
        (SlotKind.DISCHARGE_EVENING_EARLY, 47.0),
        (SlotKind.DISCHARGE_EVENING_XEARLY, 61.0),
        (SlotKind.DISCHARGE_EVENING_XXEARLY, 75.0),
    )

    _SCOPE_TODAY: Final[Scope] = "today"
    _SLOT_ORDER: Final[tuple[SlotKind, ...]] = (
        SlotKind.DISCHARGE_EVENING_XXEARLY,
        SlotKind.DISCHARGE_EVENING_XEARLY,
        SlotKind.DISCHARGE_EVENING_EARLY,
        SlotKind.DISCHARGE_EVENING_MID,
        SlotKind.DISCHARGE_EVENING_LATE,
    )

    def __init__(
        self,
        day: date,
        prices: RceDayPrices,
        *,
        is_workday: bool,
        windows: tuple[EveningWindow, ...] = (),
        slots: tuple[SlotKind, ...] = (),
        max_morning_price: float = 0.0,
        overruled_by_morning: bool = False,
        export_threshold: float = EXPORT_THRESHOLD,
    ) -> None:
        self._day = day
        self._prices = prices
        self._is_workday = is_workday
        self._windows = windows
        self._slots = slots
        self._max_morning_price = max_morning_price
        self._overruled_by_morning = overruled_by_morning
        self._export_threshold = export_threshold

    @classmethod
    def plans_tomorrow_at(
        cls,
        now: datetime,
        *,
        slots_end: time | None = None,
    ) -> bool:
        """Tell whether a run at `now` is planning tomorrow evening.

        The switchover is the end of T2 by the season (winter 21:00, summer
        22:00) — on a day off too, which has no T2 but whose evening slots may
        run later (user's decision 17.10.2026; until then a day off waited
        for 23:00). `slots_end` — the end of the last evening slot still set
        for today (`BatterySchedule.evening_end_on`) — pushes it back to the
        full hour that slot ends in: tomorrow's plan rewrites the slots, so it
        must not land while one of today's is still ahead.

        Tomorrow's windows written early cannot fire tonight: the schedule
        dates the evening (`BatterySchedule.stamp_evening`) and keeps a later
        evening's slots still until its day.

        The same rule stamps a hand edit, so an evening set by hand and an
        evening planned automatically always mean the same day.
        """
        boundary = time(evening_peak(now.date()).stop)
        if slots_end is not None:
            boundary = max(boundary, EveningPlan._full_hour_after(slots_end))
        return now.time() >= boundary

    @staticmethod
    def _full_hour_after(moment: time) -> time:
        """`moment` rounded up to a full hour; 23:59 stands in for midnight."""
        if moment.minute == 0 and moment.second == 0:
            return moment
        if moment.hour == 23:
            return time(23, 59)
        return time(moment.hour + 1)

    @classmethod
    def for_day(
        cls,
        day: date,
        prices: RceDayPrices,
        *,
        is_workday: bool,
        tomorrow: RceDayPrices | None = None,
        export_threshold: float = EXPORT_THRESHOLD,
    ) -> EveningPlan:
        """Build the plan for `day`. `tomorrow` absent = morning filter off.

        Before tomorrow's prices are published there is nothing to compare
        against, so the evening is judged on its own merits and the later run
        of the day revisits it. `export_threshold` is the user's bar
        (`number.ems_export_threshold`); EXPORT_THRESHOLD is its default.
        """
        max_morning_price = (
            tomorrow.max_gross_in(cls.MORNING_LOOKAHEAD) if tomorrow else 0.0
        )
        windows, slots = cls._windows_for(
            day, prices, is_workday, max_morning_price, export_threshold
        )
        unfiltered, _ = cls._windows_for(day, prices, is_workday, 0.0, export_threshold)
        return cls(
            day,
            prices,
            is_workday=is_workday,
            windows=windows,
            slots=slots,
            max_morning_price=max_morning_price,
            overruled_by_morning=len(windows) < len(unfiltered),
            export_threshold=export_threshold,
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
                f"no evening hour clears {self._export_threshold:.0f} "
                f"and tomorrow morning's {self._max_morning_price:.0f} (gross PLN/MWh)"
            )
        return f"no evening hour clears {self._export_threshold:.0f} gross PLN/MWh"

    def slot_commands(self) -> list[SlotCommand]:
        """Commands putting this plan into the evening slots.

        Slots without a window are disabled rather than left alone — an old
        window surviving into a day that does not want it is exactly the kind
        of stale setting this plan exists to prevent.
        """
        carried = dict(zip(self._slots, self._windows, strict=True))
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
        export_threshold: float,
    ) -> tuple[tuple[EveningWindow, ...], tuple[SlotKind, ...]]:
        """Return the windows of `day`, with the slot each one takes."""
        if is_workday:
            return cls._workday_rungs(day, prices, max_morning_price, export_threshold)
        hours = cls._best_off_peak_pair(
            prices.hours_clearing(
                cls.NON_WORKDAY_EVENING,
                at_least=export_threshold,
                above=max_morning_price,
            ),
            prices,
        )
        if not hours:
            return (), ()
        return (EveningWindow.covering(tuple(hours), prices),), (
            SlotKind.DISCHARGE_EVENING_EARLY,
        )

    @classmethod
    def _workday_rungs(
        cls,
        day: date,
        prices: RceDayPrices,
        max_morning_price: float,
        export_threshold: float,
    ) -> tuple[tuple[EveningWindow, ...], tuple[SlotKind, ...]]:
        """One rung for every T2 hour worth selling in (see the class doc)."""
        peak = evening_peak(day)
        rungs = [
            cls._rung_for(hour, peak)
            for hour in peak
            if cls._sells(hour, peak, prices, max_morning_price, export_threshold)
        ]
        return tuple(w for _k, w in rungs), tuple(k for k, _w in rungs)

    @staticmethod
    def _sells(
        hour: int,
        peak: range,
        prices: RceDayPrices,
        max_morning_price: float,
        export_threshold: float,
    ) -> bool:
        """Tell whether `hour` clears the bar, the morning and every later hour."""
        paid = prices.gross_at(hour)
        later = max(prices.gross_at(h) for h in range(hour + 1, peak.stop + 1))
        return paid >= export_threshold and paid > max_morning_price and paid >= later

    @classmethod
    def _rung_for(cls, hour: int, peak: range) -> tuple[SlotKind, EveningWindow]:
        """Return the slot and rung of a selling `hour`, by the T2 hours after it.

        Every rung but the last stands on its target as the hour closes
        (DELAYED_TO_END): the next hour is still T2 and needs that reserve.
        Hourly settlement nets the whole hour, so holding back to its end costs
        nothing. The last one empties at once.
        """
        left = peak.stop - 1 - hour
        kind, target = cls._RUNGS[left]
        behavior = SlotBehavior.IMMEDIATE if left == 0 else SlotBehavior.DELAYED_TO_END
        return kind, EveningWindow.rung(hour, target_soc=target, behavior=behavior)

    @classmethod
    def _best_off_peak_pair(
        cls, candidates: list[int], prices: RceDayPrices
    ) -> list[int]:
        """Pick the best adjacent pair of hours — the whole plan on a day off.

        With no expensive zone to reserve charge for, the only question is
        where to sell, and the battery empties in about 1h48m. Two adjacent
        hours is therefore the entire answer: a wider window only lets the
        strategy drift off the peak, and a second window would have nothing
        left to discharge.

        Falls back to the single best hour when no two qualify side by side,
        and to no window at all when nothing qualifies — which is a real
        outcome, not an edge case: the morning filter vetoes every evening
        hour whenever tomorrow's sunrise pays better.
        """
        if not candidates:
            return []
        pairs = [
            (first, second)
            for first, second in zip(candidates, candidates[1:], strict=False)
            if second == first + 1
        ]
        if pairs:
            best = max(
                pairs, key=lambda p: prices.gross_at(p[0]) + prices.gross_at(p[1])
            )
            return list(best)
        return [max(candidates, key=prices.gross_at)]


@dataclass(frozen=True)
class EveningWindow:
    """One contiguous stretch of hours to discharge into — maps onto one slot.

    Value object: `target_soc` and `behavior` are settled once, by `rung` on a
    workday and by `covering` on a day off, rather than by whoever happens to
    build the window.
    """

    # A day off has no expensive zone to ration for: its window empties.
    FULL_TARGET_SOC: ClassVar[float] = 10.0

    hours: tuple[int, ...]
    target_soc: float
    behavior: SlotBehavior

    @classmethod
    def covering(cls, hours: tuple[int, ...], prices: RceDayPrices) -> EveningWindow:
        """Build the day-off window over `hours`: it empties toward the dearer end."""
        return cls(
            hours=hours,
            target_soc=cls.FULL_TARGET_SOC,
            behavior=cls._behavior_for(hours, prices),
        )

    @classmethod
    def rung(
        cls, hour: int, *, target_soc: float, behavior: SlotBehavior
    ) -> EveningWindow:
        """One hour of the workday ladder — target and behavior come from it."""
        return cls(hours=(hour,), target_soc=target_soc, behavior=behavior)

    @property
    def start(self) -> time:
        return time(self.hours[0], 0)

    @property
    def end(self) -> time:
        """Exclusive end — the hour after the last one covered."""
        return time(self.hours[-1] + 1, 0)

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

    @staticmethod
    def _behavior_for(hours: tuple[int, ...], prices: RceDayPrices) -> SlotBehavior:
        """Push the discharge toward whichever end of the window pays more."""
        if len(hours) < 2:
            return SlotBehavior.IMMEDIATE
        if prices.gross_at(hours[0]) >= prices.gross_at(hours[-1]):
            return SlotBehavior.IMMEDIATE
        return SlotBehavior.DELAYED_TO_END
