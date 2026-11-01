"""DayOffChargeService — on a day off, charge the battery from PV or export at once.

Two runs a day off: 06:41 decides, 07:41 checks the decision on a fresher
forecast (Solcast refreshes at :40). After 07:41 the decision stands for the
day. Gathers the inputs (Live Soft PV and p60 consumption of earlier days of
the same `DayType`, RCE, the zone prices of the tariff table, SoC, S0, the
evening plan's discharge hours) and asks `DayOffChargeDecision` for the
variant; the start goes to `BatteryChargeService` (a hand-set start keeps
precedence). The 06:41 run is always reported, the 07:41 one only when the
decision changed. Rule and figures: home-assistant-ops `plans/morning-charge.md`
"Iteracja 8".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import CALLBACK_TYPE, callback

from ..domain.battery_charge_policy import DayOffStart
from ..domain.charge_start_shift import to_time
from ..domain.day_off_charge import NONE, DayOffChargeDecision, DayOffHour
from ..domain.pv_surplus import surplus_window_end
from ..tariff.rates import VAT
from ..tariff.table import latest_rates
from ..tariff.zones import zone_for

if TYPE_CHECKING:
    from .ems import Ems
    from .pv_surplus_service import PvSurplusService

DECIDE_AT: Final = time(6, 41)
VALIDATE_AT: Final = time(7, 41)
# The fallback evening when no day-off plan is written: the dearest of 16-22.
_FALLBACK_EVENING: Final = range(16, 23)


@dataclass(frozen=True)
class DayOffReport:
    """One run: the decision and what it rested on, for Telegram and the sensor."""

    at: datetime
    decision: DayOffChargeDecision
    changed: bool
    validation: bool
    manual_start: str | None
    s0_hour: int
    t2_hour: int
    soc: float
    discharge_hours: tuple[int, ...]
    surplus_kwh: float
    model_consumption_08_15_kwh: float
    day_rce_min: float
    day_rce_max: float
    evening_rce_max: float
    evening_rce_hour: int
    buy_price: float

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.decision.to_dict(),
            "evaluated_at": self.at.isoformat(),
            "run": "07:41" if self.validation else "06:41",
            "changed": self.changed,
            "manual_start": self.manual_start,
            "s0_hour": self.s0_hour,
            "t2_hour": self.t2_hour,
            "soc": self.soc,
            "discharge_hours": list(self.discharge_hours),
            "surplus_kwh": self.surplus_kwh,
            "model_consumption_08_15_kwh": self.model_consumption_08_15_kwh,
            "day_rce_min": self.day_rce_min,
            "day_rce_max": self.day_rce_max,
            "evening_rce_max": self.evening_rce_max,
            "evening_rce_hour": self.evening_rce_hour,
            "buy_price": self.buy_price,
        }


class DayOffChargeService:
    """Application service. HASS-unaware apart from the `callback` marker."""

    def __init__(
        self,
        ems: Ems,
        pv_surplus: PvSurplusService,
        notify: Callable[[DayOffReport], None],
        clock: Callable[[], datetime],
    ) -> None:
        self._ems = ems
        self._pv_surplus = pv_surplus
        self._notify = notify
        self._clock = clock
        self._listeners: dict[CALLBACK_TYPE, CALLBACK_TYPE] = {}
        self.last: DayOffReport | None = None

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        def remove_listener() -> None:
            self._listeners.pop(remove_listener)

        self._listeners[remove_listener] = update_callback
        return remove_listener

    @callback
    def evaluate(self, *, validation: bool) -> None:
        """One run (06:41 decides, 07:41 validates); nothing on a workday."""
        now = self._clock()
        today = now.date()
        if self._ems.is_workday_today:
            return
        hours = self._hours(today, now)
        state = self._ems.last_input_state
        soc = state.battery_soc if state is not None else None
        s0 = self._ems.charge_slots.today_start
        if hours is None or soc is None or s0 is None:
            return
        charge = self._ems.battery_charge_service
        held = charge.day_off_start_today
        t2 = surplus_window_end(today)
        discharge = self._discharge_hours(today, hours)
        decision = DayOffChargeDecision.decide(
            today,
            hours,
            soc=soc,
            s0_hour=s0.hour,
            t2_hour=t2,
            discharge_hours=discharge,
            in_force=held.variant if held is not None else None,
        )
        start = decision.start_minute(t2)
        charge.apply_day_off_start(
            now,
            s0,
            DayOffStart(day=today, value=to_time(start), variant=decision.chosen)
            if start is not None
            else None,
        )
        previous = (
            self.last if self.last is not None and self._is_today(self.last) else None
        )
        changed = previous is None or previous.decision.chosen != decision.chosen
        self.last = self._report(
            now, decision, changed, validation, s0.hour, t2, soc, discharge, hours
        )
        if not validation or changed:
            self._notify(self.last)
        for update_callback in list(self._listeners.values()):
            update_callback()

    def _is_today(self, report: DayOffReport) -> bool:
        return report.decision.day == self._clock().date()

    def _hours(self, today: date, now: datetime) -> list[DayOffHour] | None:
        """Today's hours: Live Soft PV, p60 consumption, RCE and zone prices (gross)."""
        rce = self._ems.rce_prices.rce_prices
        if rce is None or rce.today is None or rce.today.day != today:
            return None
        prices = rce.today.hour_price
        profile = self._pv_surplus.live_soft_profile(today, tomorrow=False)
        # Positional hour index: a 23/25-hour DST day stays out (S0 applies).
        if profile is None or len(prices) != 24:
            return None
        rates = latest_rates()
        out: list[DayOffHour] = []
        for hour in profile.hourly():
            zone = zone_for(datetime.combine(today, time(hour.hour), tzinfo=now.tzinfo))
            out.append(
                DayOffHour(
                    pv_kwh=hour.pv_kwh,
                    cons_kwh=hour.cons_kwh,
                    surplus_kwh=hour.surplus_kwh,
                    rce_gross=prices[hour.hour] * VAT / 1000,
                    buy_energy=rates.energy[zone] * VAT,
                    buy_distribution=rates.distribution[zone] * VAT,
                )
            )
        return out

    def _discharge_hours(self, today: date, hours: list[DayOffHour]) -> tuple[int, ...]:
        """Hours the evening plan sells in; without one the dearest of 16-22."""
        windows = self._ems.battery_schedule_service.evening_windows_on(today)
        planned = tuple(
            hour
            for entry in windows
            for hour in range(entry.start.hour, entry.end.hour + (entry.end.minute > 0))
        )
        if planned:
            return planned
        return tuple(sorted(_FALLBACK_EVENING, key=lambda h: -hours[h].rce_gross))

    def _report(  # noqa: PLR0913, PLR0917 — one run's figures
        self,
        now: datetime,
        decision: DayOffChargeDecision,
        changed: bool,
        validation: bool,
        s0_hour: int,
        t2_hour: int,
        soc: float,
        discharge: tuple[int, ...],
        hours: list[DayOffHour],
    ) -> DayOffReport:
        charge = self._ems.battery_charge_service
        manual = (
            charge.start_charge_hour_override
            if charge.start_charge_is_manual_today
            else None
        )
        day = range(7, t2_hour)
        evening = max(_FALLBACK_EVENING, key=lambda h: hours[h].rce_gross)
        return DayOffReport(
            at=now,
            decision=decision,
            changed=changed,
            validation=validation,
            manual_start=f"{manual:%H:%M}" if manual is not None else None,
            s0_hour=s0_hour,
            t2_hour=t2_hour,
            soc=soc,
            discharge_hours=discharge,
            surplus_kwh=round(sum(hours[h].surplus_kwh for h in day), 2),
            model_consumption_08_15_kwh=round(
                sum(hours[h].cons_kwh for h in range(8, 16)), 2
            ),
            day_rce_min=round(min(hours[h].rce_gross for h in day), 3),
            day_rce_max=round(max(hours[h].rce_gross for h in day), 3),
            evening_rce_max=round(hours[evening].rce_gross, 3),
            evening_rce_hour=evening,
            buy_price=round(
                hours[evening].buy_energy + hours[evening].buy_distribution, 3
            ),
        )


def describe(key: str) -> str:
    """Polish name of a variant, for Telegram and the dashboard."""
    if key == NONE:
        return "bez ładowania z PV"
    if key.startswith("from_"):
        return f"ładowanie od {key[5:]}:00"
    return "ładowanie od S0 (bez zmian)"
