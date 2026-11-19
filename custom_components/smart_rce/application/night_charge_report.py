"""NightChargeReport — what the workday night charge will do and why, for Telegram.

Gathers what the schedule already decided for a workday morning: the night
charge's target (the T1 reserve, lifted by the weak-sun floor or the dear day),
the dear-day choice, the morning sale plan or why there is none, and the cheap
hours behind it all. Sent as a preview with the evening plan (21:06, for
tomorrow) and again when the night charge starts and the dear-day choice is
latched (04:01, for today) — user's decision 19.11.2026. Reads only; decides
nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from typing import TYPE_CHECKING

from ..domain.battery_schedule import SlotKind
from ..domain.cheap_hours import FIRST_HOUR, cheap_hours
from ..domain.pv_surplus import surplus_window_end

if TYPE_CHECKING:
    from ..domain.battery_schedule import MorningChargeFloor, MorningDischargePlan
    from ..domain.dear_day_choice import DearDayChoice
    from .ems import Ems


@dataclass(frozen=True)
class NightChargeReport:
    """One workday morning as the schedule has it planned."""

    day: date
    latched: bool
    target: int
    reserve: int
    window: tuple[time, time]
    export_threshold: float
    first_hour: int
    t2_hour: int
    cheap: tuple[int, ...]
    floor: MorningChargeFloor | None
    dear_choice: DearDayChoice | None
    morning_plan: MorningDischargePlan | None
    morning_why_none: str | None

    @classmethod
    def build(cls, ems: Ems, day: date, *, latched: bool) -> NightChargeReport | None:
        """Build the report for `day`, None while its reserve or prices are unknown."""
        schedule = ems.battery_schedule_service
        reserve = schedule.t1_reserve_for(day)
        rce = ems.rce_prices.rce_prices
        prices = next(
            (
                p
                for p in ((rce.today, rce.tomorrow) if rce is not None else ())
                if p is not None and p.day == day
            ),
            None,
        )
        if reserve is None or prices is None:
            return None
        threshold = schedule.export_threshold
        night = schedule.slot("today", SlotKind.CHARGE_MORNING_AUTO)
        lift = schedule.night_floor_for(day)
        return cls(
            day=day,
            latched=latched,
            target=reserve.value if lift is None else lift.lifts(reserve.value),
            reserve=reserve.value,
            window=(night.start, night.end),
            export_threshold=threshold,
            first_hour=FIRST_HOUR,
            t2_hour=surplus_window_end(day),
            cheap=tuple(cheap_hours(day, prices, threshold)),
            floor=schedule.morning_charge_floor_for(day),
            dear_choice=schedule.dear_day_choice_for(day),
            morning_plan=schedule.morning_discharge_for(day),
            morning_why_none=schedule.morning_discharge_why_none_for(day),
        )
