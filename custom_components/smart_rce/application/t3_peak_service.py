"""T3PeakService — recomputes today's T3 sale every minute, and previews tomorrow's.

Gathers the day's prices, the PV surplus (Live Soft, p60 consumption), the zone
prices and the battery projected to the sale's first hour, and hands
`BatterySchedule` a `T3PeakPlan` — or None, which keeps DISCHARGE_T3_PEAK
still and leaves CHARGE_AFTERNOON as the user set it. When its first hour
comes the plan is latched (persisted): from then on the battery level reflects
the plan itself, and a sale recomputed from it mid-way would decide on the
half-sold battery.

- A winter workday sells at 13:00 or 14:00 and buys back before T2 (user's
  decision 10.11.2026; `T3PeakEvaluation.evaluate`).
- A day off — the whole day T3 — sells in the day's dearest hour after a charge
  the hour before (user's decision 25.11.2026; `evaluate_day_off`).

Tomorrow's plan is worked out too, once its prices are out, so the chart shows
it the evening before (user, 25.11.2026). It is never latched: on the day the
plan is recomputed from the battery as it stands. The preview assumes a full
battery at 13:00 on a workday (the high-price days that sell are dear days,
charged to 100% overnight) and the floor at dawn on a day off (the house draws
the battery overnight), the day's PV on top.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final

from homeassistant.core import callback

from ..domain.battery_schedule import SlotKind, T3PeakEvaluation, T3PeakHour, T3PeakPlan
from ..domain.charge_curve import kwh_per_pp
from ..domain.evening_plan import EveningPlan
from ..tariff.rates import VAT, Zone
from ..tariff.table import latest_rates
from ..tariff.zones import evening_peak, is_winter, zone_for

if TYPE_CHECKING:
    from ..domain.pv_surplus import SurplusProfile
    from ..domain.rce import RceDayPrices
    from .ems import Ems
    from .pv_surplus_service import PvSurplusService

_FIRST_HOUR: Final[int] = 13
# A workday plan may still start this late after a restart (user, 10.11.2026:
# HA down 12:50-13:10 must not lose the sale).
_LATEST_START: Final[time] = time(13, 15)
# A day-off sale may still start this many minutes into its hour.
_DAY_OFF_LATEST_MINUTE: Final[int] = 15
_FULL: Final[float] = 100.0


class T3PeakService:
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
        """Rebuild today's plan (or keep the latched one), then tomorrow's preview."""
        now = self._clock()
        self._refresh_today(now)
        self._preview_tomorrow(now)

    def _refresh_today(self, now: datetime) -> None:
        today = now.date()
        schedule = self._ems.battery_schedule_service
        if schedule.t3_peak_latched(today):
            schedule.set_t3_peak_report(today, None, None)
            return
        if self._ems.is_workday_today:
            evaluation, why = self._evaluate_workday(now, today)
        else:
            evaluation, why = self._evaluate_day_off(now, today)
        schedule.set_t3_peak_report(today, evaluation, why)
        plan = evaluation.plan if evaluation is not None else None
        schedule.set_t3_peak(today, plan)
        if plan is not None and now.time() >= plan.latch_at:
            schedule.latch_t3_peak(plan)

    def _evaluate_workday(
        self, now: datetime, today: date
    ) -> tuple[T3PeakEvaluation | None, str | None]:
        """Today's workday evaluation, or None and why there is none (Polish)."""
        if not is_winter(today):
            return None, "tylko zimowy dzień roboczy albo dzień wolny"
        if now.time() >= _LATEST_START:
            # No plan by 13:15 (or HA was down longer): the sale's hour is gone.
            return None, "po 13:15 bez planu"
        if now.hour >= evening_peak(today).start - 1:
            return None, "za późno na sprzedaż i doładowanie przed T2"
        inputs = self._inputs(today, tomorrow=False)
        if inputs is None:
            return None, "brak cen, prognozy PV albo SOC"
        prices, profile, soc = inputs
        hours = self._hours(today, prices, profile, now)
        start = now.hour * 60 + now.minute
        return self._workday(today, hours, self._drained(profile, start, soc)), None

    def _inputs(
        self, day: date, *, tomorrow: bool
    ) -> tuple[RceDayPrices, SurplusProfile, float] | None:
        prices = self._prices_on(day, tomorrow=tomorrow)
        profile = self._pv_surplus.live_soft_profile(day, tomorrow=tomorrow)
        state = self._ems.last_input_state
        soc = state.battery_soc if state is not None else None
        if prices is None or profile is None or soc is None:
            return None
        return prices, profile, soc

    def _prices_on(self, day: date, *, tomorrow: bool) -> RceDayPrices | None:
        rce = self._ems.rce_prices.rce_prices
        prices = None if rce is None else rce.tomorrow if tomorrow else rce.today
        if prices is None or prices.day != day or len(prices.hour_price) != 24:
            return None
        return prices

    @staticmethod
    def _hours(
        day: date, prices: RceDayPrices, profile: SurplusProfile, now: datetime
    ) -> list[T3PeakHour]:
        rates = latest_rates()
        return [
            T3PeakHour(
                surplus_kwh=profile.total(h * 60, (h + 1) * 60),
                deficit_kwh=profile.deficit(h * 60, (h + 1) * 60),
                rce_gross=prices.gross_at(h) / 1000,
                buy_energy=rates.energy[z] * VAT,
                buy_distribution=rates.distribution[z] * VAT,
            )
            for h in range(24)
            for z in (zone_for(datetime.combine(day, time(h), tzinfo=now.tzinfo)),)
        ]

    @staticmethod
    def _drained(profile: SurplusProfile, start: int, soc: float) -> float:
        """Project the battery to 13:00: from `start` less the house's T1 drain."""
        end = _FIRST_HOUR * 60
        if start >= end:
            return soc
        drain = profile.deficit(start, end)
        return max(10.0, soc - drain / kwh_per_pp(soc))

    def _workday(
        self, day: date, hours: list[T3PeakHour], soc: float
    ) -> T3PeakEvaluation:
        t2 = evening_peak(day)
        charge = self._ems.battery_schedule_service.slot(
            "today", SlotKind.CHARGE_AFTERNOON
        )
        rates = latest_rates()
        return T3PeakEvaluation.evaluate(
            day,
            hours,
            soc=soc,
            t2_start=t2.start,
            charge_end_minute=charge.end.hour * 60 + charge.end.minute,
            t2_hours=range(t2.start, t2.stop),
            t2_buy=(rates.energy[Zone.T2] + rates.distribution[Zone.T2]) * VAT,
        )

    def _evaluate_day_off(
        self, now: datetime, today: date
    ) -> tuple[T3PeakEvaluation | None, str | None]:
        """Today's day-off evaluation, or None and why there is none (Polish)."""
        inputs = self._inputs(today, tomorrow=False)
        if inputs is None:
            return None, "brak cen, prognozy PV albo SOC"
        prices, profile, soc = inputs
        hours = self._hours(today, prices, profile, now)
        sell = self._dearest_hour(hours)
        if now.time() >= time(sell, _DAY_OFF_LATEST_MINUTE):
            return None, f"najdroższa godzina dnia ({sell}:00) już minęła"
        start = now.hour * 60 + now.minute
        soc_at = self._with_pv(profile, start, (sell - 1) * 60, soc)
        return self._day_off(today, prices, hours, sell, soc_at), None

    @staticmethod
    def _dearest_hour(hours: list[T3PeakHour]) -> int:
        """Pick the day's dearest hour that has an hour before it to charge in."""
        return max(range(1, 24), key=lambda h: hours[h].rce_gross)

    @staticmethod
    def _with_pv(profile: SurplusProfile, start: int, end: int, soc: float) -> float:
        """Project the battery from `start` to `end`: the PV in, the house out."""
        if start >= end:
            return soc
        net = profile.total(start, end) - profile.deficit(start, end)
        return min(_FULL, max(10.0, soc + net / kwh_per_pp(soc)))

    def _day_off(
        self,
        day: date,
        prices: RceDayPrices,
        hours: list[T3PeakHour],
        sell: int,
        soc: float,
    ) -> T3PeakEvaluation:
        return T3PeakEvaluation.evaluate_day_off(
            day,
            hours,
            soc=soc,
            sell_hour=sell,
            moved_value=self._moved_value(prices, hours, sell),
        )

    def _moved_value(
        self, prices: RceDayPrices, hours: list[T3PeakHour], sell: int
    ) -> float:
        """Value the battery's energy without the sale (zl/kWh gross).

        The day-off pair sells it in the dearest hours that clear the export
        threshold; without such hours the house takes it at the T3 price.
        """
        threshold = self._ems.battery_schedule_service.export_threshold
        clearing = prices.hours_clearing(
            EveningPlan.NON_WORKDAY_SALE_HOURS, at_least=threshold
        )
        buy = hours[sell].buy_energy + hours[sell].buy_distribution
        return max([buy, *(hours[h].rce_gross for h in clearing)])

    def _preview_tomorrow(self, now: datetime) -> None:
        """Tomorrow's plan for the chart, once its prices are out; never latched."""
        tomorrow = now.date() + timedelta(days=1)
        schedule = self._ems.battery_schedule_service
        schedule.set_t3_peak(tomorrow, self._tomorrow_plan(now, tomorrow))

    def _tomorrow_plan(self, now: datetime, tomorrow: date) -> T3PeakPlan | None:
        inputs = self._inputs(tomorrow, tomorrow=True)
        if inputs is None:
            return None
        prices, profile, _soc = inputs
        hours = self._hours(tomorrow, prices, profile, now)
        if self._ems.is_workday_tomorrow:
            if not is_winter(tomorrow):
                return None
            return self._workday(tomorrow, hours, _FULL).plan
        sell = self._dearest_hour(hours)
        soc = self._with_pv(profile, 0, (sell - 1) * 60, 10.0)
        return self._day_off(tomorrow, prices, hours, sell, soc).plan
