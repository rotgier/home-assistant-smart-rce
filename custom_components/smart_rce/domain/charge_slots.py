"""ChargeSlots — algorytm doboru godzin tanich + cached today/tomorrow ChargeWindow.

Aggregate root dla decyzji "kiedy ładować baterię z PV". Trzyma cached
state (`today`, `tomorrow` ChargeWindow) i zna algorytm doboru godzin
operujący na RCE prices.

Forward-looking: przy integracji prognozy pogody + Solcast w przyszłości,
`ChargeSlots.update` przyjmie dodatkowe źródła i będzie naturalnym miejscem
na multi-source decision. Pojedynczy entity `RcePrices` (pure data) by
puchł — aggregate koordynujący wiele źródeł lepiej pasuje pod DDD.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import logging
from statistics import mean
from typing import Final

from .rce import TIMEZONE, RceDayPrices, RcePrices

_LOGGER = logging.getLogger(__name__)

MAX_CONSECUTIVE_HOURS: Final[int] = 8

# Defaults for the user-tunable charge-window parameters (ChargeWindowParams).
# They reproduce the historical hardcoded behaviour: base window = 3 h, extend
# earlier when the prior hour is < 45 zł/MWh above the base-window max OR cheap
# in absolute terms (< 100 zł/MWh), base-window start shifted 30 min earlier.
DEFAULT_INITIAL_HOURS: Final[int] = 3
DEFAULT_EXTEND_THRESHOLD: Final[float] = 45.0
DEFAULT_ABSOLUTE_CHEAP_PRICE: Final[float] = 100.0
DEFAULT_BASE_WINDOW_SHIFT_MIN: Final[int] = 30

EARLIEST_CHARGE_HOUR: Final[int] = 7
SHIFT_EARLIER_THRESHOLD: Final[float] = 40.0

# Default heater RCE threshold (net zł/MWh) — must match heater automations
# in home-assistant-config/automations.yaml. Used as fallback when
# input_number.heater_rce_threshold state is unavailable.
DEFAULT_HEATER_RCE_THRESHOLD: Final[float] = 350.0

# Skip shift_earlier_if_cheap when heaters are effectively blocked all day
# (≤ this many hours below threshold). Rationale: if heaters can't fire,
# the local sink for surplus PV reduces to battery capacity (fixed) — widening
# the charge window doesn't increase total PV absorbed locally.
MAX_HEATERS_OFF_TOLERANCE: Final[int] = 3


@dataclass(frozen=True)
class ChargeWindow:
    """Computed charge window for one day — projection na sensor.py."""

    start_hour: float  # non-integer when base-window shift applies (best_n == initial)
    start_datetime: datetime
    end_hour: float
    end_datetime: datetime


@dataclass(frozen=True)
class ChargeWindowParams:
    """User-tunable inputs to the charge-window selection algorithm.

    Sourced from `BatteryChargePolicy` (dashboard select + numbers). Defaults
    reproduce the historical hardcoded behaviour, so `compute(day)` without
    params is a regression-safe no-op vs the previous algorithm.

    - `initial_hours` — base window length; algorithm extends earlier/longer
      from here when worthwhile.
    - `extend_threshold` — take an earlier+longer window when the earlier hour
      is at most this many zł/MWh above the base-window max price.
    - `absolute_cheap_price` — ...or when that earlier hour is cheap outright.
    - `base_window_shift_minutes` — when the chosen window equals the base
      length, start this many minutes earlier (0 = no shift).
    """

    initial_hours: int = DEFAULT_INITIAL_HOURS
    extend_threshold: float = DEFAULT_EXTEND_THRESHOLD
    absolute_cheap_price: float = DEFAULT_ABSOLUTE_CHEAP_PRICE
    base_window_shift_minutes: int = DEFAULT_BASE_WINDOW_SHIFT_MIN


@dataclass(frozen=True)
class StartChargeTodayChanged:
    """Event emitted by ChargeSlots when today's start time-of-day changes.

    Emitted from:
    - `update` — only when the newly-computed today_start differs from the
      previous value (RCE refresh that genuinely changed the slot).
    - `rotate_if_day_changed` — on every rotation regardless of equality
      (semantic: 'new day = fresh value, downstream can decide stickiness').
    """

    new_value: time


class ChargeSlots:
    """Cached today/tomorrow ChargeWindow + algorytm doboru godzin."""

    def __init__(self) -> None:
        self.today: ChargeWindow | None = None
        self.tomorrow: ChargeWindow | None = None

    def update(
        self,
        rce_data: RcePrices | None,
        heater_threshold: float = DEFAULT_HEATER_RCE_THRESHOLD,
        params: ChargeWindowParams | None = None,
    ) -> StartChargeTodayChanged | None:
        """Recompute charge windows from RCE — full refresh today/tomorrow.

        Returns `StartChargeTodayChanged` event when today's start_datetime
        time-of-day actually changed (Etap B'-2 — drives auto-sync of
        BatteryChargePolicy.start_charge_hour_override).

        `params` (ChargeWindowParams) carries the user-tunable knobs from
        BatteryChargePolicy via Ems. None = library defaults.
        """
        params = params or ChargeWindowParams()
        previous = self.today.start_datetime.time() if self.today else None
        if rce_data is None:
            self.today = None
            self.tomorrow = None
            new = None
        else:
            self.today = self.compute(rce_data.today, heater_threshold, params)
            self.tomorrow = self.compute(rce_data.tomorrow, heater_threshold, params)
            new = self.today.start_datetime.time() if self.today else None
        if new is not None and new != previous:
            return StartChargeTodayChanged(new_value=new)
        return None

    def rotate_if_day_changed(self, now: datetime) -> StartChargeTodayChanged | None:
        """Move tomorrow → today when date rolled. Returns event on rotation.

        Event is emitted on EVERY rotation (not gated on value equality with
        previous today) — semantic: 'new day, fresh value'. Downstream
        consumer (BatteryChargeService.auto_sync_start_charge_hour_override)
        handles idempotent no-op when value matches the current override.
        """
        if (
            self.today is not None
            and self.tomorrow is not None
            and self.today.start_datetime.date() != now.date()
        ):
            _LOGGER.info(
                "Rotating charge slots: tomorrow → today (today was %s, now is %s)",
                self.today.start_datetime.date(),
                now.date(),
            )
            self.today = self.tomorrow
            self.tomorrow = None
            new = self.today.start_datetime.time() if self.today else None
            if new is not None:
                return StartChargeTodayChanged(new_value=new)
        return None

    @staticmethod
    def compute(
        day_prices: RceDayPrices | None,
        heater_threshold: float = DEFAULT_HEATER_RCE_THRESHOLD,  # noqa: ARG004
        params: ChargeWindowParams | None = None,
    ) -> ChargeWindow | None:
        """Dobór okna ładowania dla pojedynczego dnia (parametryzowany).

        Starts from `params.initial_hours` and may take an earlier+longer
        window when the earlier hour is cheap (< absolute_cheap_price) or only
        marginally above the base-window max (< extend_threshold). When the
        chosen window equals the base length, the start is shifted earlier by
        `params.base_window_shift_minutes`. Start is searched in `range(6, 16)`.
        """
        if day_prices is None or not day_prices.hour_price:
            return None
        params = params or ChargeWindowParams()
        prices: list[float] = list(day_prices.hour_price)
        start_charge_hours = calculate_start_charge_hours(prices, params.initial_hours)
        # NOTE 2026-05-29: shift_earlier_if_cheap disabled. Anchor-based
        # heuristic (prices[end] as benchmark) misfires on bimodal price
        # profiles where find_best picks N for low mean but last hour is
        # an outlier. Function kept (CSV diagnostic fixture still exercises it).
        best_n = find_best_consecutive_hours(
            prices,
            start_charge_hours,
            params.initial_hours,
            params.extend_threshold,
            params.absolute_cheap_price,
        )
        start = start_charge_hours[best_n]
        # When the window stays at the base length, start earlier by the
        # configured shift (gives margin if the PV forecast under-delivers).
        if best_n == params.initial_hours:
            start_hour = start - params.base_window_shift_minutes / 60
        else:
            start_hour = float(start)
        end_hour = float(start + best_n)
        return ChargeWindow(
            start_hour=start_hour,
            start_datetime=_hour_to_datetime(day_prices.day, start_hour),
            end_hour=end_hour,
            end_datetime=_hour_to_datetime(day_prices.day, end_hour),
        )


def _hour_to_datetime(day: date, hour: float) -> datetime:
    minute = int(hour * 60 % 60)
    return datetime.combine(day, time(int(hour), minute, 0), TIMEZONE)


def calculate_start_charge_hours(
    prices: list[float], initial_hours: int = DEFAULT_INITIAL_HOURS
) -> dict[int, int]:
    return {
        consecutive_hours: _cheapest_start_for_length(prices, consecutive_hours)
        for consecutive_hours in range(initial_hours, MAX_CONSECUTIVE_HOURS + 1)
    }


def _cheapest_start_for_length(prices: list[float], consecutive_hours: int) -> int:
    """Start hour in `range(6, 16)` minimizing the mean of an N-hour window."""
    min_avg = float("inf")
    best_hour = 0
    for hour in range(6, 16):
        avg = mean(prices[hour : hour + consecutive_hours])
        if avg < min_avg:
            min_avg = avg
            best_hour = hour
    return best_hour


def find_best_consecutive_hours(
    prices: list[float],
    start_charge_hours: dict[int, int],
    initial_hours: int = DEFAULT_INITIAL_HOURS,
    extend_threshold: float = DEFAULT_EXTEND_THRESHOLD,
    absolute_cheap_price: float = DEFAULT_ABSOLUTE_CHEAP_PRICE,
) -> int:
    best_consecutive_hours = initial_hours
    best_hour: int = start_charge_hours[best_consecutive_hours]

    initial_consecutive_hours_max_price = max(
        prices[best_hour : best_hour + best_consecutive_hours]
    )
    for consecutive_hours in range(initial_hours + 1, MAX_CONSECUTIVE_HOURS + 1):
        candidate: int = start_charge_hours[consecutive_hours]
        if (
            candidate == best_hour
            or candidate < best_hour
            and (
                prices[candidate] < absolute_cheap_price
                or prices[candidate] - initial_consecutive_hours_max_price
                < extend_threshold
            )
        ):
            best_consecutive_hours = consecutive_hours

    return best_consecutive_hours


def shift_earlier_if_cheap(
    prices: list[float],
    start: int,
    consecutive_hours: int,
    heater_threshold: float = DEFAULT_HEATER_RCE_THRESHOLD,
) -> tuple[int, int]:
    """Rozszerz okno ładowania wcześniej gdy różnica cen z kotwicą jest mała.

    Zachowujemy ostatnią godzinę oryginalnego okna (nadal tania, brak powodu
    żeby ją gubić) i dokładamy wcześniejsze godziny. Daje margines na
    załamanie pogody po południu oraz pozwala grzałkom dłużej drink'ować PV
    surplus (heater automation requires `battery_charge_current > 0`,
    czyli aktywne charge window).

    Kotwica (prices[end]) się nie zmienia między iteracjami, więc próg nie
    kumuluje się przy kolejnych krokach.

    Nie ograniczamy do MAX_CONSECUTIVE_HOURS — to limit głównego selectora
    (find_best_consecutive_hours), a tutaj rozszerzamy okno o godziny tak
    tanie że nie ma powodu ich nie dorzucić (przykład: weekend z cenami ~0).
    EARLIEST_CHARGE_HOUR jest jedynym twardym hamulcem.

    Skip-shift on high-RCE days: when ≤ MAX_HEATERS_OFF_TOLERANCE hours have
    price below `heater_threshold`, heaters are effectively blocked all day.
    Local sink for surplus PV reduces to battery capacity (fixed) — widening
    the window doesn't increase total PV absorbed. Skip the shift.

    Zwraca (nowe_consecutive_hours, nowy_start).
    """
    heater_hours = sum(1 for p in prices if p < heater_threshold)
    if heater_hours <= MAX_HEATERS_OFF_TOLERANCE:
        return consecutive_hours, start
    end = start + consecutive_hours - 1
    anchor_price = prices[end]
    while start > EARLIEST_CHARGE_HOUR:
        if prices[start - 1] - anchor_price > SHIFT_EARLIER_THRESHOLD:
            break
        start -= 1
        consecutive_hours += 1
    return consecutive_hours, start
