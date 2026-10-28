"""ChargeStartShiftService — evaluates the forecast-driven charge start every 5 min.

Gathers the inputs (S0 from `ChargeSlots`, today's hourly prices, SoC, the
LIVE_SOFT surplus per variant, from 09:10 Extrap Proportional), asks the domain for
a candidate per variant and hands the applied one (GUARDED: p60 consumption
of earlier days of the same type, earlier moves held back until Extrap agrees) to `BatteryChargeService`, which owns and
persists the shift. The other variants are only tracked here, in memory, so
the sensor can show where each of them would have put the start.

Runs from 07:41 until the start in force (S0, or later when the start moved
past it). A confirmed move is reported — also when a hand-set start keeps it
from taking effect. Only the applied variant looks for a cheaper later start
(`cheaper_later_start`, since 28.10.2026); the observed ones, Extrap and the
rewind watch keep the rule up to S0. Once the start is locked the shift
stops moving; from 09:00 a `RewindWatch` then looks for a later start that
would still fill the battery and raises an alert (phone call) — the user
decides, nothing is changed automatically.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time
import logging
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, callback

from ..domain.charge_curve import kwh_to_full
from ..domain.charge_start_shift import (
    APPLIED_VARIANT,
    EXTRAP_FROM,
    FIRST_EVALUATION,
    REWIND_FROM,
    Candidate,
    ChargeStartShift,
    RewindWatch,
    ShiftVariant,
    cheaper_later_start,
    find_candidate,
    guarded_candidate,
    price_window_end,
    to_minutes,
)
from ..domain.pv_forecast import PvForecast
from ..domain.pv_surplus import surplus_window_end

if TYPE_CHECKING:
    from ..domain.pv_surplus import SurplusProfile
    from .battery_charge_service import BatteryChargeService, ForecastShiftMove
    from .ems import Ems
    from .pv_surplus_service import PvSurplusService

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShiftReport:
    """A confirmed move with what the forecast said at that moment."""

    move: ForecastShiftMove
    s0: int
    soc: float
    need_kwh: float
    surplus_kwh: float
    window_end_hour: int


@dataclass(frozen=True)
class RewindReport:
    """Charging under way, yet a later start would still fill the battery."""

    start: int
    soc: float
    need_kwh: float
    suggestion: int
    live_kwh: float
    extrap_kwh: float
    window_end_hour: int
    alert_no: int
    max_alerts: int


@dataclass(frozen=True)
class ShiftEvaluation:
    """Last evaluation, for the sensor."""

    at: datetime
    soc: float
    need_kwh: float
    candidates: dict[ShiftVariant, int]
    extrap: int | None
    # Start each PV forecast asks for with the p60 consumption — observed
    # only, to see which forecast would have moved the start when.
    by_pv: dict[PvForecast, Candidate] = field(default_factory=dict)


class ChargeStartShiftService:
    """Application service. HASS-unaware — the notifier is injected."""

    def __init__(
        self,
        ems: Ems,
        pv_surplus: PvSurplusService,
        notify: Callable[[ShiftReport], None],
        alert: Callable[[RewindReport], None],
        clock: Callable[[], datetime],
    ) -> None:
        self._ems = ems
        self._charge: BatteryChargeService = ems.battery_charge_service
        self._pv_surplus = pv_surplus
        self._notify_move = notify
        self._alert_rewind = alert
        self._rewind: RewindWatch | None = None
        self._clock = clock
        self._listeners: dict[CALLBACK_TYPE, CALLBACK_TYPE] = {}
        self._observed: dict[ShiftVariant, ChargeStartShift] = {}
        self.last: ShiftEvaluation | None = None

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        def remove_listener() -> None:
            self._listeners.pop(remove_listener)

        self._listeners[remove_listener] = update_callback
        return remove_listener

    @property
    def shift(self) -> ChargeStartShift | None:
        return self._charge.forecast_shift

    @property
    def rewind(self) -> RewindWatch | None:
        """Today's rewind watch (None until the start locked after 09:00)."""
        watch = self._rewind
        if watch is None or watch.day != self._clock().date():
            return None
        return watch

    @property
    def manual_start_today(self) -> str | None:
        """The hand-set start in force today instead of the shift, or None."""
        if not self._charge.start_charge_is_manual_today:
            return None
        start = self._charge.start_charge_hour_override
        return f"{start:%H:%M}" if start is not None else None

    def observed_start(self, variant: ShiftVariant) -> int | None:
        """Where an observed variant would have put today's start."""
        if variant is APPLIED_VARIANT:
            shift = self.shift
            return shift.start if shift is not None else None
        tracker = self._observed.get(variant)
        return tracker.start if tracker is not None else None

    @callback
    def evaluate(self) -> None:
        """One evaluation (scheduled at :01, :06, :11 ...)."""
        now = self._clock()
        s0 = self._ems.charge_slots.today_start
        if s0 is None or not self._in_window(now, s0, self.shift):
            return
        state = self._ems.last_input_state
        soc = state.battery_soc if state is not None else None
        prices = self._today_prices(now.date())
        if soc is None or prices is None:
            return
        shift = self.shift
        if shift is not None and shift.locked:
            self._watch_rewind(now, shift, soc, prices, to_minutes(s0))
            self._notify_listeners()
            return
        now_min, s0_min, need = to_minutes(now.time()), to_minutes(s0), kwh_to_full(soc)
        candidates: dict[ShiftVariant, int] = {}
        # Past S0 (a start moved later) the observed rule only answers "now".
        observing = now_min < s0_min
        for variant in ShiftVariant:
            if variant.guarded or not observing:
                continue
            profile = self._pv_surplus.today_profile(
                history=variant.history, pv_scale=variant.pv_scale
            )
            if profile is not None:
                found = find_candidate(profile, prices, now_min, need, s0_min)
                candidates[variant] = found.start
        applied = self._applied_profile()
        if applied is None:
            return
        live = cheaper_later_start(
            applied,
            prices,
            now_min,
            find_candidate(applied, prices, now_min, need, s0_min),
            # Until a later start the battery stays closed and covers the house.
            lambda start: need + applied.deficit(now_min, start),
            window_end=surplus_window_end(now.date()) * 60,
        )
        found_extrap = self._extrap_candidate(
            prices, now_min, need, s0_min, EXTRAP_FROM
        )
        extrap = found_extrap.start if found_extrap is not None else None
        current = shift.start if shift is not None else s0_min
        candidates[APPLIED_VARIANT] = guarded_candidate(live, extrap, current, now_min)
        self.last = ShiftEvaluation(
            now,
            soc,
            need,
            candidates,
            extrap,
            self._by_pv_variant(prices, now_min, need, s0_min) if observing else {},
        )
        self._observe(now.date(), now_min, s0_min, candidates)
        move = self._charge.evaluate_forecast_shift(
            now, s0, candidates[APPLIED_VARIANT], soc
        )
        if move is not None:
            self._report(move, s0_min, soc, need, prices)
        self._notify_listeners()

    @staticmethod
    def _in_window(now: datetime, s0: time, shift: ChargeStartShift | None) -> bool:
        """From 07:41 until the start — S0, or today's start moved past it."""
        end = to_minutes(s0)
        if shift is not None and shift.day == now.date():
            end = max(end, shift.start)
        return FIRST_EVALUATION <= to_minutes(now.time()) < end

    def _watch_rewind(
        self,
        now: datetime,
        shift: ChargeStartShift,
        soc: float,
        prices: tuple[float, ...],
        s0_min: int,
    ) -> None:
        """Locked start: alert when a later one would still fill the battery."""
        now_min = to_minutes(now.time())
        if now_min < REWIND_FROM or self._charge.start_charge_is_manual_today:
            return
        profile = self._applied_profile()
        if profile is None:
            return
        need = kwh_to_full(soc)
        live = find_candidate(profile, prices, now_min, need, s0_min)
        extrap = self._extrap_candidate(prices, now_min, need, s0_min, REWIND_FROM)
        watch = self._rewind
        if watch is None or watch.day != now.date():
            watch = self._rewind = RewindWatch(now.date())
        suggestion = watch.evaluate(now_min, live, extrap)
        if suggestion is None:
            return
        end = price_window_end(suggestion, prices)
        extrap_profile = self._applied_profile(PvForecast.EXTRAP_PROPORTIONAL)
        _LOGGER.info(
            "Charge rewind alert %d: charging since %d min, later start %d min",
            watch.alerts,
            shift.start,
            suggestion,
        )
        self._alert_rewind(
            RewindReport(
                start=shift.start,
                soc=soc,
                need_kwh=need,
                suggestion=suggestion,
                live_kwh=profile.total(suggestion, end * 60),
                extrap_kwh=(
                    extrap_profile.total(suggestion, end * 60)
                    if extrap_profile is not None
                    else 0.0
                ),
                window_end_hour=end,
                alert_no=watch.alerts,
                max_alerts=RewindWatch.MAX_ALERTS_PER_DAY,
            )
        )

    def _extrap_candidate(
        self,
        prices: tuple[float, ...],
        now_min: int,
        need: float,
        s0_min: int,
        since: int,
    ) -> Candidate | None:
        """Extrap Proportional's candidate (flat 0.9), from `since` on."""
        if now_min < since:
            return None
        profile = self._applied_profile(PvForecast.EXTRAP_PROPORTIONAL)
        if profile is None:
            return None
        return find_candidate(profile, prices, now_min, need, s0_min)

    def _by_pv_variant(
        self, prices: tuple[float, ...], now_min: int, need: float, s0_min: int
    ) -> dict[PvForecast, Candidate]:
        """Each today PV forecast's candidate with the applied consumption model."""
        out: dict[PvForecast, Candidate] = {}
        for variant in PvForecast.today():
            profile = self._applied_profile(variant)
            if profile is not None:
                out[variant] = find_candidate(profile, prices, now_min, need, s0_min)
        return out

    def _applied_profile(
        self, source: PvForecast = PvForecast.LIVE_SOFT
    ) -> SurplusProfile | None:
        """Surplus the applied variant reads.

        p60 consumption of earlier days of the same type; flat 0.9 kWh/h
        until that history is loaded.
        """
        profile = self._pv_surplus.today_profile(
            history=APPLIED_VARIANT.history,
            pv_scale=APPLIED_VARIANT.pv_scale,
            source=source,
        )
        if profile is None:
            profile = self._pv_surplus.today_profile(
                history=False, pv_scale=APPLIED_VARIANT.pv_scale, source=source
            )
        return profile

    def _today_prices(self, today: date) -> tuple[float, ...] | None:
        rce = self._ems.rce_prices.rce_prices
        if rce is None or rce.today is None or rce.today.day != today:
            return None
        prices = rce.today.hour_price
        # Positional hour index: on a 23/25-hour DST day it does not line up
        # with the clock, so the shift deliberately stays off (S0 applies).
        return prices if len(prices) == 24 else None

    def _observe(
        self,
        today: date,
        now_min: int,
        s0_min: int,
        candidates: dict[ShiftVariant, int],
    ) -> None:
        """Track the variants that are not applied (no lock — they never charge)."""
        for variant, candidate in candidates.items():
            if variant is APPLIED_VARIANT:
                continue
            tracker = self._observed.get(variant)
            if tracker is None or tracker.day != today:
                tracker = self._observed[variant] = ChargeStartShift(
                    day=today, s0=s0_min
                )
            elif tracker.s0 != s0_min:
                tracker.restart(s0_min)
            tracker.evaluate(now_min, candidate, None)

    def _report(
        self,
        move: ForecastShiftMove,
        s0_min: int,
        soc: float,
        need: float,
        prices: tuple[float, ...],
    ) -> None:
        profile = self._applied_profile()
        end = price_window_end(move.after, prices)
        surplus = profile.total(move.after, end * 60) if profile is not None else 0.0
        _LOGGER.info(
            "Charge start moved by forecast: %d -> %d min (S0 %d, manual %s)",
            move.before,
            move.after,
            s0_min,
            move.manual_start,
        )
        self._notify_move(
            ShiftReport(
                move=move,
                s0=s0_min,
                soc=soc,
                need_kwh=need,
                surplus_kwh=surplus,
                window_end_hour=end,
            )
        )

    def _notify_listeners(self) -> None:
        for update_callback in list(self._listeners.values()):
            update_callback()
