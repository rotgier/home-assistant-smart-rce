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
INITIAL_BEST_CONSECUTIVE_HOURS: Final[int] = 3
POSSIBLE_CONSECUTIVE_HOURS: Final[range] = range(3, MAX_CONSECUTIVE_HOURS + 1)
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

    start_hour: float  # może być 8.5 przy half-hour shift (best_n == 3)
    start_datetime: datetime
    end_hour: float
    end_datetime: datetime


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
        charge_hours_override: int | None = None,
    ) -> StartChargeTodayChanged | None:
        """Recompute charge windows from RCE — full refresh today/tomorrow.

        Returns `StartChargeTodayChanged` event when today's start_datetime
        time-of-day actually changed (Etap B'-2 — drives auto-sync of
        BatteryChargePolicy.start_charge_hour_override).

        `charge_hours_override` (None = Auto) forces a fixed-length charge
        window — see `compute`. Fed from `BatteryChargePolicy` via Ems.
        """
        previous = self.today.start_datetime.time() if self.today else None
        if rce_data is None:
            self.today = None
            self.tomorrow = None
            new = None
        else:
            self.today = self.compute(
                rce_data.today, heater_threshold, charge_hours_override
            )
            self.tomorrow = self.compute(
                rce_data.tomorrow, heater_threshold, charge_hours_override
            )
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
        charge_hours_override: int | None = None,
    ) -> ChargeWindow | None:
        """Algorytm: dobór najlepszego okna ładowania dla pojedynczego dnia.

        `charge_hours_override` (None = Auto): when set, forces a fixed-length
        window of N consecutive cheapest-on-average hours instead of the
        adaptive 3–8 h selection. Half-hour shift is skipped (integer start).
        Start is still searched within the normal `range(6, 16)`.
        """
        if day_prices is None or not day_prices.hour_price:
            return None
        prices: list[float] = list(day_prices.hour_price)
        if charge_hours_override is not None:
            start_hour, end_hour = ChargeSlots._forced_window(
                prices, charge_hours_override
            )
        else:
            start_hour, end_hour = ChargeSlots._adaptive_window(prices)
        return ChargeWindow(
            start_hour=start_hour,
            start_datetime=_hour_to_datetime(day_prices.day, start_hour),
            end_hour=end_hour,
            end_datetime=_hour_to_datetime(day_prices.day, end_hour),
        )

    @staticmethod
    def _forced_window(prices: list[float], hours: int) -> tuple[float, float]:
        """Fixed-length override: N cheapest-on-average consecutive hours."""
        start = _cheapest_start_for_length(prices, hours)
        return float(start), float(start + hours)

    @staticmethod
    def _adaptive_window(prices: list[float]) -> tuple[float, float]:
        """Auto path: adaptive 3–8 h selection by lowest mean price."""
        start_charge_hours = calculate_start_charge_hours(prices)
        best_n = find_best_consecutive_hours(prices, start_charge_hours)
        # NOTE 2026-05-29: shift_earlier_if_cheap disabled. Anchor-based
        # heuristic (prices[end] as benchmark) misfires on bimodal price
        # profiles where find_best picks N for low mean but last hour is
        # an outlier (e.g. 2026-05-29: window [10-17] mean=8 with h17=460
        # — shift dorzucił h09=260 PLN/MWh blocking PV export at ~32 gr/kWh).
        # Rework planned: skip shift when ≥3 cheap hours in original window
        # AND weather forecast 1-2h before window start is partly-cloudy
        # or better (most reliable forecast horizon). Function kept (unit
        # tests + CSV diagnostic fixture still exercise it pure).
        shifted_start = start_charge_hours[best_n]
        # Half-hour shift dla N=3: bateria startuje w połowie pierwszej godziny.
        # Dla N>3 (po shift) start_hour to plain integer.
        if best_n == INITIAL_BEST_CONSECUTIVE_HOURS:
            start_hour = shifted_start - 0.5
        else:
            start_hour = float(shifted_start)
        end_hour = float(shifted_start + best_n)
        return start_hour, end_hour


def _hour_to_datetime(day: date, hour: float) -> datetime:
    minute = int(hour * 60 % 60)
    return datetime.combine(day, time(int(hour), minute, 0), TIMEZONE)


def calculate_start_charge_hours(prices: list[float]) -> dict[int, int]:
    return {
        consecutive_hours: _cheapest_start_for_length(prices, consecutive_hours)
        for consecutive_hours in POSSIBLE_CONSECUTIVE_HOURS
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
    prices: list[float], start_charge_hours: dict[int, int]
) -> int:
    best_consecutive_hours = INITIAL_BEST_CONSECUTIVE_HOURS
    best_hour: int = start_charge_hours[best_consecutive_hours]

    initial_consecutive_hours_max_price = max(
        prices[best_hour : best_hour + best_consecutive_hours]
    )
    hours_to_check = filter(
        lambda x: x > INITIAL_BEST_CONSECUTIVE_HOURS, POSSIBLE_CONSECUTIVE_HOURS
    )
    for consecutive_hours in hours_to_check:
        candidate: int = start_charge_hours[consecutive_hours]
        if (
            candidate == best_hour
            or candidate < best_hour
            and (
                prices[candidate] < 100
                or prices[candidate] - initial_consecutive_hours_max_price < 45
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
