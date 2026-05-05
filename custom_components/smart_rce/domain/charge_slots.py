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

from .rce import TIMEZONE, RceData, RceDayPrices

_LOGGER = logging.getLogger(__name__)

MAX_CONSECUTIVE_HOURS: Final[int] = 8
INITIAL_BEST_CONSECUTIVE_HOURS: Final[int] = 3
POSSIBLE_CONSECUTIVE_HOURS: Final[range] = range(3, MAX_CONSECUTIVE_HOURS + 1)
EARLIEST_CHARGE_HOUR: Final[int] = 7
SHIFT_EARLIER_THRESHOLD: Final[float] = 40.0


@dataclass(frozen=True)
class ChargeWindow:
    """Computed charge window for one day — projection na sensor.py."""

    start_hour: float  # może być 8.5 przy half-hour shift (best_n == 3)
    start_datetime: datetime
    end_hour: float
    end_datetime: datetime


class ChargeSlots:
    """Cached today/tomorrow ChargeWindow + algorytm doboru godzin."""

    def __init__(self) -> None:
        self.today: ChargeWindow | None = None
        self.tomorrow: ChargeWindow | None = None

    def update(self, rce_data: RceData | None) -> None:
        """Recompute charge windows z fresh RCE data — full refresh today/tomorrow."""
        if rce_data is None:
            self.today = None
            self.tomorrow = None
            return
        self.today = self.compute(rce_data.today)
        self.tomorrow = self.compute(rce_data.tomorrow)

    def rotate_if_day_changed(self, now: datetime) -> None:
        """Move tomorrow → today gdy data się zmieniła (wywoływane z update_hourly)."""
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

    @staticmethod
    def compute(day_prices: RceDayPrices | None) -> ChargeWindow | None:
        """Algorytm: dobór najlepszego okna ładowania dla pojedynczego dnia."""
        if day_prices is None or not day_prices.prices:
            return None
        prices: list[float] = [item["price"] for item in day_prices.prices]
        start_charge_hours = calculate_start_charge_hours(prices)
        best_n = find_best_consecutive_hours(prices, start_charge_hours)
        new_n, shifted_start = shift_earlier_if_cheap(
            prices, start_charge_hours[best_n], best_n
        )
        # Half-hour shift dla N=3: bateria startuje w połowie pierwszej godziny.
        # Dla N>3 (po shift) start_hour to plain integer.
        if new_n == INITIAL_BEST_CONSECUTIVE_HOURS:
            start_hour = shifted_start - 0.5
        else:
            start_hour = float(shifted_start)
        end_hour = float(shifted_start + new_n)
        day = day_prices.prices[0]["datetime"].date()
        return ChargeWindow(
            start_hour=start_hour,
            start_datetime=_hour_to_datetime(day, start_hour),
            end_hour=end_hour,
            end_datetime=_hour_to_datetime(day, end_hour),
        )


def _hour_to_datetime(day: date, hour: float) -> datetime:
    minute = int(hour * 60 % 60)
    return datetime.combine(day, time(int(hour), minute, 0), TIMEZONE)


def calculate_start_charge_hours(prices: list[float]) -> dict[int, int]:
    start_charge_hours: dict[int, int] = {}
    for consecutive_hours in POSSIBLE_CONSECUTIVE_HOURS:
        min_avg = float("inf")
        best_hour = 0
        for hour in range(6, 16):
            avg = mean(prices[hour : hour + consecutive_hours])
            if avg < min_avg:
                min_avg = avg
                best_hour = hour
        start_charge_hours[consecutive_hours] = best_hour
    return start_charge_hours


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
    prices: list[float], start: int, consecutive_hours: int
) -> tuple[int, int]:
    """Rozszerz okno ładowania wcześniej gdy różnica cen z kotwicą jest mała.

    Zachowujemy ostatnią godzinę oryginalnego okna (nadal tania, brak powodu
    żeby ją gubić) i dokładamy wcześniejsze godziny. Daje margines na
    załamanie pogody po południu.

    Kotwica (prices[end]) się nie zmienia między iteracjami, więc próg nie
    kumuluje się przy kolejnych krokach.

    Nie ograniczamy do MAX_CONSECUTIVE_HOURS — to limit głównego selectora
    (find_best_consecutive_hours), a tutaj rozszerzamy okno o godziny tak
    tanie że nie ma powodu ich nie dorzucić (przykład: weekend z cenami ~0).
    EARLIEST_CHARGE_HOUR jest jedynym twardym hamulcem.

    Zwraca (nowe_consecutive_hours, nowy_start).
    """
    end = start + consecutive_hours - 1
    anchor_price = prices[end]
    while start > EARLIEST_CHARGE_HOUR:
        if prices[start - 1] - anchor_price > SHIFT_EARLIER_THRESHOLD:
            break
        start -= 1
        consecutive_hours += 1
    return consecutive_hours, start
