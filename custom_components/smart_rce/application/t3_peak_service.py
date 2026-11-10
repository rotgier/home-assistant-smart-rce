"""T3PeakService — recomputes today's afternoon T3 sale every minute.

Gathers today's prices, the PV surplus (Live Soft, p60 consumption), the zone
prices and the battery projected to 13:00, and hands `BatterySchedule` a
`T3PeakPlan` — or None, which keeps DISCHARGE_T3_PEAK still and leaves
CHARGE_AFTERNOON as the user set it. From 13:00 the plan is latched (persisted):
from then on the battery level reflects the plan itself, and a sale recomputed
from it at 14:30 would decide on the half-sold battery. Winter workdays only
(T2 from 16:00, three T3 hours before it). The rule lives in the domain
(`T3PeakEvaluation.evaluate`; user's decision 10.11.2026).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Final

from homeassistant.core import callback

from ..domain.battery_schedule import SlotKind, T3PeakEvaluation, T3PeakHour
from ..domain.charge_curve import kwh_per_pp
from ..tariff.rates import VAT, Zone
from ..tariff.table import latest_rates
from ..tariff.zones import evening_peak, is_winter, zone_for

if TYPE_CHECKING:
    from ..domain.pv_surplus import SurplusProfile
    from ..domain.rce import RceDayPrices
    from .ems import Ems
    from .pv_surplus_service import PvSurplusService

_FIRST_HOUR: Final[int] = 13
# A plan may still start this late after a restart (user, 10.11.2026: HA down
# 12:50-13:10 must not lose the sale).
_LATEST_START: Final[time] = time(13, 15)


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
        """Rebuild today's plan (or keep the latched one) and hand it on."""
        now = self._clock()
        today = now.date()
        schedule = self._ems.battery_schedule_service
        if schedule.t3_peak_latched(today):
            schedule.set_t3_peak_report(today, None, None)
            return
        if now.time() >= _LATEST_START:
            # No plan by 13:15 (or HA was down longer): the sale's hour is gone.
            schedule.set_t3_peak_report(today, None, "po 13:15 bez planu")
            schedule.set_t3_peak(today, None)
            return
        evaluation, why = self._evaluate(now, today)
        schedule.set_t3_peak_report(today, evaluation, why)
        plan = evaluation.plan if evaluation is not None else None
        schedule.set_t3_peak(today, plan)
        if plan is not None and now.time() >= time(_FIRST_HOUR):
            schedule.latch_t3_peak(plan)

    def _evaluate(
        self, now: datetime, today: date
    ) -> tuple[T3PeakEvaluation | None, str | None]:
        """Today's evaluation, or None and why there is none (Polish, dashboard)."""
        t2 = evening_peak(today)
        if not self._ems.is_workday_today or not is_winter(today):
            return None, "tylko zimowy dzień roboczy"
        if now.hour >= t2.start - 1:
            return None, "za późno na sprzedaż i doładowanie przed T2"
        prices = self._prices_today(today)
        profile = self._pv_surplus.live_soft_profile(today, tomorrow=False)
        state = self._ems.last_input_state
        soc = state.battery_soc if state is not None else None
        if prices is None or profile is None or soc is None:
            return None, "brak cen, prognozy PV albo SOC"
        charge = self._ems.battery_schedule_service.slot(
            "today", SlotKind.CHARGE_AFTERNOON
        )
        rates = latest_rates()
        hours = [
            T3PeakHour(
                surplus_kwh=profile.total(h * 60, (h + 1) * 60),
                deficit_kwh=profile.deficit(h * 60, (h + 1) * 60),
                rce_gross=prices.gross_at(h) / 1000,
                buy_energy=rates.energy[z] * VAT,
                buy_distribution=rates.distribution[z] * VAT,
            )
            for h in range(24)
            for z in (zone_for(datetime.combine(today, time(h), tzinfo=now.tzinfo)),)
        ]
        evaluation = T3PeakEvaluation.evaluate(
            today,
            hours,
            soc=self._soc_at_first_hour(profile, now, soc),
            t2_start=t2.start,
            charge_end_minute=charge.end.hour * 60 + charge.end.minute,
            t2_hours=range(t2.start, t2.stop),
            t2_buy=(rates.energy[Zone.T2] + rates.distribution[Zone.T2]) * VAT,
        )
        return evaluation, None

    @staticmethod
    def _soc_at_first_hour(profile: SurplusProfile, now: datetime, soc: float) -> float:
        """Project the battery to 13:00: now less the house's T1 drain to come."""
        start = now.hour * 60 + now.minute
        end = _FIRST_HOUR * 60
        if start >= end:
            return soc
        drain = profile.deficit(start, end)
        return max(10.0, soc - drain / kwh_per_pp(soc))

    def _prices_today(self, today: date) -> RceDayPrices | None:
        rce = self._ems.rce_prices.rce_prices
        prices = rce.today if rce is not None else None
        if prices is None or prices.day != today or len(prices.hour_price) != 24:
            return None
        return prices
