"""MorningDischargeService — recomputes the automatic morning discharge.

Every minute, like the target of the auto morning charge: gathers the plan
day's prices, S0, the T1 reserve (a day off: the 10% floor) and the PV surplus
(Live Soft, p60 consumption of earlier days of the same kind) and hands
`BatterySchedule` a `MorningDischargePlan` — or None, which keeps the
DISCHARGE_MORNING_AUTO slot still. Both today's and tomorrow's plan, the latter
as soon as its inputs exist (tomorrow's prices ~14:00; for a workday also
tomorrow's T1 reserve, from 14:00): the evening preview shows it, and the hold
rule (DodPolicy) keeps the battery for a morning sale (user, 17.10.2026). The rule itself lives in the domain (`MorningDischargePlan.decide`,
`lowest_soc_the_sun_fills`).

A dear workday runs the plan only when `DearDayChoice` finds the normal day
worth `MIN_ADVANTAGE_ZL` more than keeping the night's full battery (user,
08.11.2026); the choice also tells the night charge to stop at the reserve
(`BatterySchedule.night_floor_for`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import callback

from ..domain.battery_schedule import MorningDischargePlan, SlotKind, is_day_off
from ..domain.charge_start_shift import (
    full_charge_minute,
    lowest_soc_the_sun_fills,
    to_minutes,
)
from ..domain.dear_day_choice import DearDayChoice, DearDayHour, DearDayVariant
from ..domain.pv_surplus import surplus_window_end
from ..domain.target_soc import MIN_SOC_PERCENT
from ..tariff.rates import VAT
from ..tariff.table import latest_rates
from ..tariff.zones import zone_for

if TYPE_CHECKING:
    from ..domain.battery_schedule import BatteryScheduleEntry, T1SocReserve
    from ..domain.rce import RceDayPrices
    from .ems import Ems
    from .pv_surplus_service import PvSurplusService


class MorningDischargeService:
    """Application service. HASS-unaware apart from the `callback` marker."""

    def __init__(
        self,
        ems: Ems,
        pv_surplus: PvSurplusService,
        clock: Callable[[], datetime],
    ) -> None:
        self._ems = ems
        self._pv_surplus = pv_surplus
        self._clock = clock

    @callback
    def refresh(self) -> None:
        """Rebuild today's and tomorrow's plan and hand them to the schedule."""
        now = self._clock()
        schedule = self._ems.battery_schedule_service
        for day in (now.date(), now.date() + timedelta(days=1)):
            plan, why_none = self._plan(now, day)
            schedule.set_morning_discharge(day, plan)
            schedule.set_morning_discharge_why_none(day, why_none)

    def _plan(
        self, now: datetime, day: date
    ) -> tuple[MorningDischargePlan | None, str | None]:
        """`day`'s plan (today or tomorrow), or None and why there is none.

        Tomorrow's plan takes over at midnight; both are recomputed every
        minute, so they follow the forecast. The reason is for the dashboard
        (Polish, as the user reads it).
        """
        schedule = self._ems.battery_schedule_service
        tomorrow = day != now.date()
        is_workday = (
            self._ems.is_workday_tomorrow if tomorrow else self._ems.is_workday_today
        )
        entry = schedule.slot("today", SlotKind.DISCHARGE_MORNING_AUTO)
        # Computed with the slot switched off too — the sensor then shows what
        # it would do; the schedule runs a plan only for an enabled slot.
        if not SlotKind.DISCHARGE_MORNING_AUTO.profile.runs_on(day, is_workday):
            return None, "slot nie działa w ten dzień"
        day_off = is_day_off(day, is_workday)
        prices = self._prices_for(day)
        s0 = (
            self._ems.charge_slots.tomorrow_start
            if tomorrow
            else self._ems.charge_slots.today_start
        )
        reserve = schedule.t1_reserve
        if prices is None:
            return None, "brak cen RCE na ten dzień"
        if s0 is None:
            return None, "brak startu ładowania z cen RCE"
        if not day_off and (reserve is None or reserve.day != day):
            return None, "brak rezerwy T1 na ten dzień (liczona od 14:00 dnia przed)"
        plan, why_none = self._normal_plan(
            now, day, entry, prices, s0, reserve, day_off=day_off, tomorrow=tomorrow
        )
        floor = schedule.morning_charge_floor_for(day)
        if floor is None or not floor.dear_day or floor.target is None:
            schedule.set_dear_day_choice(day, None)
            return plan, why_none
        choice = self._dear_day_choice(now, day, plan, prices, s0, reserve, tomorrow)
        schedule.set_dear_day_choice(day, choice)
        if choice is not None and choice.chosen is DearDayVariant.NORMAL:
            return plan, why_none
        # A full battery pays better (or the choice lacks an input): the night
        # charge fills it for T1 and the sun goes to the grid all day.
        why = (
            f"drogi dzień (wszystkie godziny 07–{surplus_window_end(day)}:00 "
            f"od {schedule.export_threshold:.0f} brutto): nocne ładowanie do "
            f"{floor.target}% zostaje w baterii"
        )
        return None, why if choice is None else f"{why}; {choice.describe()}"

    def _normal_plan(  # noqa: PLR0913, PLR0917 — the plan day's inputs, one caller
        self,
        now: datetime,
        day: date,
        entry: BatteryScheduleEntry,
        prices: RceDayPrices,
        s0: time,
        reserve: T1SocReserve | None,
        *,
        day_off: bool,
        tomorrow: bool,
    ) -> tuple[MorningDischargePlan | None, str | None]:
        """`day`'s plan as on a normal day, or None and why there is none."""
        schedule = self._ems.battery_schedule_service
        # A day off searches from 05:00 whatever the slot's start says: the
        # slot's window is tuned for workdays.
        first = (
            MorningDischargePlan.EARLIEST_HOUR_DAY_OFF
            if day_off
            else self._first_hour(entry.start.hour, entry.start.minute)
        )
        best = MorningDischargePlan.best_hour(
            prices, first, min(entry.end.hour, s0.hour), day_off=day_off
        )
        if best is None:
            return None, (
                f"start ładowania o {s0:%H:%M} — przed nim nie ma godziny od {first:02d}:00"
            )
        profile = self._pv_surplus.live_soft_profile(day, tomorrow=tomorrow)
        if profile is None:
            return None, "brak prognozy PV (Live Soft)"
        t2 = surplus_window_end(day)
        # Starts from the reading without margin — the lowest the battery can
        # be left at, so the latest full charge (the cautious side).
        start_soc = (
            MIN_SOC_PERCENT
            if day_off or reserve is None
            else (
                reserve.value_without_margin
                if reserve.value_without_margin is not None
                else reserve.value
            )
        )
        full = full_charge_minute(profile, to_minutes(s0), start_soc, t2 * 60)
        # The sun charges the battery until the hour it fills in (inclusive).
        refill_end = t2 if full is None else min(t2, -(-full // 60))
        hours = MorningDischargePlan.hours_from(prices, best, s0.hour, refill_end)
        if not hours:
            return None, self._outbid(prices, best, s0.hour, refill_end, full)
        # The hour running now or the next one still to come (tomorrow: the first).
        hour = next((h for h in hours if tomorrow or h >= now.hour), None)
        if hour is None:
            hhmm = ", ".join(f"{h:02d}:00" for h in hours)
            return None, f"godziny sprzedaży już minęły ({hhmm})"
        # The line the hour stands on: the margin step at the hour's start,
        # not now's (at 07:41 the plan for 08:00 already takes the 08 step).
        # A day off has no T1 — the floor.
        line = (
            MIN_SOC_PERCENT
            if day_off or reserve is None
            else reserve.value_at(datetime.combine(day, time(hour), tzinfo=now.tzinfo))
        )
        fill = lowest_soc_the_sun_fills(
            profile, prices.hour_price, (hour + 1) * 60, to_minutes(s0), line
        )
        plan = MorningDischargePlan.decide(
            export_threshold=schedule.export_threshold,
            day=day,
            hour=hour,
            gross_price=prices.gross_at(hour),
            reserve=line,
            fill=fill,
            hours=hours,
            full_at_minute=full,
            day_off=day_off,
        )
        return plan, None

    @staticmethod
    def _outbid(
        prices: RceDayPrices,
        best: int,
        s0_hour: int,
        refill_end: int,
        full: int | None,
    ) -> str:
        """Why even the dearest morning hour does not sell: a later hour pays more.

        The energy sold in the morning comes back from the sun in the hours up
        to the full charge (`MorningDischargePlan.hours_from`); an hour paying
        more among them, or before the charge start, is worth more.
        """
        later = range(best + 1, max(refill_end, s0_hour))
        dearer = max(later, key=prices.gross_at)
        sun = (
            f"słońce dopełni baterię ok. {full // 60:02d}:{full % 60:02d}"
            if full is not None
            else f"prognoza nie dopełnia baterii przed T2 ({refill_end:02d}:00)"
        )
        return (
            f"najdroższa {best:02d}:00 ({prices.gross_at(best):.0f}) nie przebija "
            f"{dearer:02d}:00 ({prices.gross_at(dearer):.0f}) — tę energię lepiej "
            f"zachować; {sun}"
        )

    @staticmethod
    def _first_hour(hour: int, minute: int) -> int:
        """First whole hour inside the slot's window."""
        return hour + (1 if minute else 0)

    def _dear_day_choice(  # noqa: PLR0913, PLR0917 — the plan day's inputs, one caller
        self,
        now: datetime,
        day: date,
        plan: MorningDischargePlan | None,
        prices: RceDayPrices,
        s0: time,
        reserve: T1SocReserve | None,
        tomorrow: bool,
    ) -> DearDayChoice | None:
        """Choose a full battery or a normal day — None while an input is missing.

        Valued from the battery as it is now (the evening's level, before the
        night charge). Latched when the night charge starts: from then on the
        level reflects the choice itself, and a full night recomputed at 06:00
        would sell at 07:00 what the night just bought.
        """
        schedule = self._ems.battery_schedule_service
        state = self._ems.last_input_state
        soc = state.battery_soc if state is not None else None
        profile = self._pv_surplus.live_soft_profile(day, tomorrow=tomorrow)
        if soc is None or profile is None or reserve is None:
            return None
        night = schedule.slot("today", SlotKind.CHARGE_MORNING_AUTO)
        rates = latest_rates()
        hours = []
        for h in range(24):
            zone = zone_for(datetime.combine(day, time(h), tzinfo=now.tzinfo))
            hours.append(
                DearDayHour(
                    surplus_kwh=profile.total(h * 60, (h + 1) * 60),
                    deficit_kwh=profile.deficit(h * 60, (h + 1) * 60),
                    rce_gross=prices.gross_at(h) / 1000,
                    buy_energy=rates.energy[zone] * VAT,
                    buy_distribution=rates.distribution[zone] * VAT,
                )
            )
        choice = DearDayChoice.decide(
            day,
            hours,
            soc=soc,
            night_start_hour=night.start.hour,
            night_end_hour=night.end.hour,
            reserve=reserve.value,
            s0_hour=s0.hour,
            t2_hour=surplus_window_end(day),
            sale_hours=plan.hours if plan is not None else (),
            sale_target=plan.target if plan is not None else None,
        )
        if now < datetime.combine(day, night.start, tzinfo=now.tzinfo):
            return choice
        latched = schedule.dear_day_latched(day)
        if latched is None:
            schedule.latch_dear_day(day, choice.chosen.value)
            return choice
        return choice.with_chosen(DearDayVariant(latched))

    def _prices_for(self, day: date) -> RceDayPrices | None:
        rce = self._ems.rce_prices.rce_prices
        if rce is None:
            return None
        prices = next(
            (p for p in (rce.today, rce.tomorrow) if p is not None and p.day == day),
            None,
        )
        # Positional hour index: a 23/25-hour DST day does not line up with the
        # clock, so the plan stays off then.
        return prices if prices is not None and len(prices.hour_price) == 24 else None
