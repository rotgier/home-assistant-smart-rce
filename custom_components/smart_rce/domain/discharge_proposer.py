"""Proposes the evening discharge windows from RCE prices and the G13 zone.

Replaces an evening ritual: read tomorrow's prices, find the expensive hours,
decide whether they beat the next morning, then translate all that into slot
windows and targets. The rules below are the user's own, written down.

An hour qualifies for discharging when BOTH hold (gross PLN/MWh throughout):

1. `price >= EXPORT_THRESHOLD` (750). Below it the spread over a night-time
   T3 purchase (626) is too thin to be worth emptying the battery for — the
   stored energy does more good feeding the house overnight.
2. `price > the best price tomorrow morning`. The same kWh can be sold once;
   if the morning pays better, the evening waits.

Qualifying hours are then grouped into up to two windows, because a peak
followed by a dip and another expensive hour needs a hold in between. When a
later expensive hour remains inside T2, the first window stops at
`PARTIAL_TARGET_SOC` so the house still runs off the battery instead of
buying at the T2 rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from typing import Final

from ..const import GROSS_MULTIPLIER
from .battery_schedule import SlotBehavior, SlotKind
from .rce import RceDayPrices
from .tariff import G13Zone

# Gross PLN/MWh below which we would rather keep the charge for the house.
EXPORT_THRESHOLD: Final[float] = 750.0

# Hours scanned for "is tomorrow morning worth more than tonight". Wider than
# `discharge_slots.MORNING_DISCHARGE_*` (5-8) on purpose: that constant picks
# the best hour to discharge INTO, before PV starts. Here the question is only
# whether the morning outbids the evening, and from autumn to late winter 08:00
# still produces almost nothing, so it belongs to the morning for our purposes.
MORNING_LOOKAHEAD: Final[range] = range(5, 9)

# Evening hours considered on days with no expensive zone (weekends, holidays).
NON_WORKDAY_EVENING: Final[range] = range(16, 23)

# Left in the battery by the first window when an expensive hour still follows.
PARTIAL_TARGET_SOC: Final[float] = 33.0
FULL_TARGET_SOC: Final[float] = 10.0

# A run this long outlasts one discharge, so it is worth two windows.
_MIN_HOURS_TO_SPLIT: Final[int] = 3


@dataclass(frozen=True)
class ProposedSlot:
    """One window the proposer wants written into the schedule."""

    kind: SlotKind
    start: time
    end: time
    target_soc: float
    behavior: SlotBehavior


@dataclass(frozen=True)
class DischargeProposal:
    """What to do this evening — possibly nothing.

    `slots` is empty when no hour qualifies; the caller then disables both
    evening slots rather than leaving yesterday's windows in place.
    """

    slots: tuple[ProposedSlot, ...]
    reason: str

    @property
    def is_empty(self) -> bool:
        return not self.slots


def propose_evening_discharge(
    *,
    today: RceDayPrices,
    tomorrow: RceDayPrices | None,
    day: date,
    is_workday: bool,
) -> DischargeProposal:
    """Build the evening plan for `day` from its prices and tomorrow's morning."""
    morning_bid = _best_morning_price(tomorrow)
    candidates = _qualifying_hours(today, day, is_workday, morning_bid)
    if not candidates:
        return DischargeProposal(slots=(), reason=_no_slot_reason(morning_bid))
    groups = _split_long_run(_contiguous_groups(candidates))
    tail = _peak_window_end(day, is_workday)

    return DischargeProposal(
        slots=_slots_for(groups, today, tail),
        reason=f"qualifying hours: {', '.join(f'{h:02d}' for h in candidates)}",
    )


def _no_slot_reason(morning_bid: float) -> str:
    """Why nothing was proposed — shown in the Telegram summary."""
    if morning_bid > 0:
        return (
            f"no evening hour clears {EXPORT_THRESHOLD:.0f} "
            f"and tomorrow morning's {morning_bid:.0f} (gross PLN/MWh)"
        )
    return f"no evening hour clears {EXPORT_THRESHOLD:.0f} gross PLN/MWh"


def _best_morning_price(tomorrow: RceDayPrices | None) -> float:
    """Highest gross price in tomorrow's morning window; 0 when unknown.

    Zero means "nothing to beat" — before tomorrow's prices are published the
    evening plan is built on tonight's merits alone, and the second run of the
    day revisits it once they land.
    """
    if tomorrow is None or not tomorrow.hour_price:
        return 0.0
    prices = [
        tomorrow.hour_price[h] * GROSS_MULTIPLIER
        for h in MORNING_LOOKAHEAD
        if h < len(tomorrow.hour_price)
    ]
    return max(prices, default=0.0)


def _qualifying_hours(
    today: RceDayPrices, day: date, is_workday: bool, morning_bid: float
) -> list[int]:
    """Evening hours clearing both the absolute threshold and the morning."""
    return [
        hour
        for hour in _evening_hours(day, is_workday)
        if hour < len(today.hour_price)
        and (price := today.hour_price[hour] * GROSS_MULTIPLIER) >= EXPORT_THRESHOLD
        and price > morning_bid
    ]


def _evening_hours(day: date, is_workday: bool) -> range:
    """Hours to consider — the T2 block on workdays, a broad evening otherwise."""
    if not is_workday:
        return NON_WORKDAY_EVENING
    start, end = G13Zone.evening_peak_hours(day)
    return range(start, end)


def _contiguous_groups(hours: list[int]) -> list[list[int]]:
    """Split qualifying hours into runs, so a cheap hour in between breaks them."""
    groups: list[list[int]] = []
    for hour in hours:
        if groups and hour == groups[-1][-1] + 1:
            groups[-1].append(hour)
        else:
            groups.append([hour])
    return groups


def _split_long_run(groups: list[list[int]]) -> list[list[int]]:
    """Break a single run of three-plus hours into a head and a final hour.

    A full battery takes roughly 1h48m to empty, so one window spanning three
    expensive hours would cover only two of them — IMMEDIATE would skip the
    last, DELAYED_TO_END the first. Splitting lets the head stop at the
    partial target and the tail finish the job, putting energy into all three.

    Only an undivided run is split; when prices already produced two groups
    there are no slots left to give.
    """
    if len(groups) != 1 or len(groups[0]) < _MIN_HOURS_TO_SPLIT:
        return groups
    run = groups[0]
    return [run[:-1], run[-1:]]


def _peak_window_end(day: date, is_workday: bool) -> int | None:
    """Hour at which T2 ends, or None when the day has no expensive zone.

    None means weekends and holidays, which are T3 around the clock: there is
    no later expensive hour to hold charge for, so every window may empty the
    battery.
    """
    if not is_workday:
        return None
    return G13Zone.evening_peak_hours(day)[1]


def _slots_for(
    groups: list[list[int]], today: RceDayPrices, tail: int | None
) -> tuple[ProposedSlot, ...]:
    """Turn hour runs into at most two slots, EARLY first then LATE.

    Only the first two runs are used: the battery does not hold enough for a
    third window, and the schedule has two evening slots by design.
    """
    kinds = (SlotKind.DISCHARGE_EVENING_EARLY, SlotKind.DISCHARGE_EVENING_LATE)
    usable = groups[: len(kinds)]
    slots = []
    for index, (kind, hours) in enumerate(zip(kinds, usable, strict=False)):
        is_last = index == len(usable) - 1
        slots.append(
            ProposedSlot(
                kind=kind,
                start=time(hours[0], 0),
                end=time(hours[-1] + 1, 0),
                target_soc=_target_for(hours, tail, is_last=is_last),
                behavior=_behavior_for(hours, today),
            )
        )
    return tuple(slots)


def _target_for(hours: list[int], tail: int | None, *, is_last: bool) -> float:
    """Reserve charge only while an expensive hour still lies ahead.

    Holding back makes sense when the window ends before T2 does, because the
    house would otherwise buy those hours at the T2 rate. Three cases empty
    the battery instead:

    - no expensive zone at all (weekend, holiday) — nothing to reserve for;
    - the window runs to the end of T2 — likewise nothing left to cover;
    - the window is a single hour — at full charge the target is out of reach
      anyway (~50 pp/h against 90 pp to give), and when the battery starts low
      we would rather sell everything into that one expensive hour than ration
      it.

    An earlier window always holds back: the later one is what empties.
    """
    if not is_last:
        return PARTIAL_TARGET_SOC
    if tail is None or len(hours) < 2 or hours[-1] + 1 >= tail:
        return FULL_TARGET_SOC
    return PARTIAL_TARGET_SOC


def _behavior_for(hours: list[int], today: RceDayPrices) -> SlotBehavior:
    """Push the discharge toward whichever end of the window pays more."""
    if len(hours) < 2:
        return SlotBehavior.IMMEDIATE
    first = today.hour_price[hours[0]]
    last = today.hour_price[hours[-1]]
    return SlotBehavior.IMMEDIATE if first >= last else SlotBehavior.DELAYED_TO_END
