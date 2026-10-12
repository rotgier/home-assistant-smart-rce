"""ChargeStartShiftService — evaluates the forecast-driven charge start every 5 min.

Gathers the inputs (S0 from `ChargeSlots`, today's hourly prices, SoC, the
LIVE surplus per variant), asks the domain for a candidate per variant and
hands the applied one (0.9 kWh/h) to `BatteryChargeService`, which owns and
persists the shift. The other variants are only tracked here, in memory, so
the sensor can show where each of them would have put the start.

Runs from 07:41 until S0 or until the start is locked. A confirmed move is
reported — also when a hand-set start keeps it from taking effect.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
import logging
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, callback

from ..domain.charge_curve import kwh_to_full
from ..domain.charge_start_shift import (
    FIRST_EVALUATION,
    ChargeStartShift,
    ShiftVariant,
    candidate_start,
    price_window_end,
    to_minutes,
)

if TYPE_CHECKING:
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
class ShiftEvaluation:
    """Last evaluation, for the sensor."""

    at: datetime
    soc: float
    need_kwh: float
    candidates: dict[ShiftVariant, int]


class ChargeStartShiftService:
    """Application service. HASS-unaware — the notifier is injected."""

    def __init__(
        self,
        ems: Ems,
        pv_surplus: PvSurplusService,
        notify: Callable[[ShiftReport], None],
        clock: Callable[[], datetime],
    ) -> None:
        self._ems = ems
        self._charge: BatteryChargeService = ems.battery_charge_service
        self._pv_surplus = pv_surplus
        self._notify_move = notify
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
    def manual_start_today(self) -> str | None:
        """The hand-set start in force today instead of the shift, or None."""
        if not self._charge.start_charge_is_manual_today:
            return None
        start = self._charge.start_charge_hour_override
        return f"{start:%H:%M}" if start is not None else None

    def observed_start(self, variant: ShiftVariant) -> int | None:
        """Where an observed variant would have put today's start."""
        if variant is ShiftVariant.FLAT:
            shift = self.shift
            return shift.start if shift is not None else None
        tracker = self._observed.get(variant)
        return tracker.start if tracker is not None else None

    @callback
    def evaluate(self) -> None:
        """One evaluation (scheduled at :01, :06, :11 ...)."""
        now = self._clock()
        s0 = self._ems.charge_slots.today_start
        if s0 is None or not self._due(now, s0):
            return
        state = self._ems.last_input_state
        soc = state.battery_soc if state is not None else None
        prices = self._today_prices(now.date())
        if soc is None or prices is None:
            return
        now_min, s0_min, need = to_minutes(now.time()), to_minutes(s0), kwh_to_full(soc)
        candidates: dict[ShiftVariant, int] = {}
        for variant in ShiftVariant:
            profile = self._pv_surplus.today_profile(
                median=variant.median, pv_scale=variant.pv_scale
            )
            if profile is not None:
                candidates[variant] = candidate_start(
                    profile, prices, now_min, need, s0_min
                )
        if ShiftVariant.FLAT not in candidates:
            return
        self.last = ShiftEvaluation(now, soc, need, candidates)
        self._observe(now.date(), now_min, s0_min, candidates)
        move = self._charge.evaluate_forecast_shift(
            now, s0, candidates[ShiftVariant.FLAT], soc
        )
        if move is not None:
            self._report(move, s0_min, soc, need, prices)
        self._notify_listeners()

    def _due(self, now: datetime, s0: time) -> bool:
        """From 07:41 until S0, unless the start is already locked."""
        now_min = to_minutes(now.time())
        if not FIRST_EVALUATION <= now_min < to_minutes(s0):
            return False
        shift = self.shift
        return shift is None or not shift.locked

    def _today_prices(self, today: date) -> tuple[float, ...] | None:
        rce = self._ems.rce_prices.rce_prices
        if rce is None or rce.today is None or rce.today.day != today:
            return None
        prices = rce.today.hour_price
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
            if variant is ShiftVariant.FLAT:
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
        profile = self._pv_surplus.today_profile(median=False, pv_scale=1.0)
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
