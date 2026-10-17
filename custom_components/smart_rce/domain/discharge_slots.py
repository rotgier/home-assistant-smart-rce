"""DischargeSlots — algorytm peaków RCE + cached state.

Aggregate dla decyzji "kiedy rozładować baterię w peak ceny".
Trzyma cached field `max_upcoming_peak`, recomputowany przy `update_rce`
i `update_hourly` w `Ems`.

Sensor.py czyta jako field (`ems.discharge_slots.max_upcoming_peak`),
nie metodę — zero domain-specific arguments wyciekających do warstwy
ekspozycji.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .rce import RceDayPrices, RcePrices


@dataclass(frozen=True, kw_only=True)
class UpcomingPeak:
    """Najwyższa cena RCE w nadchodzących drogich oknach.

    Wieczór dziś (19-22) lub rano jutro (6-9). Zwracane raw rce_pln
    (PLN/MWh, netto) — konwersja brutto odbywa się w warstwie sensorów.
    """

    price: float
    datetime: datetime


@dataclass
class DischargeSlots:
    """Cached discharge timing — recomputed on update_rce + update_hourly."""

    max_upcoming_peak: UpcomingPeak | None = None

    def update(self, rce_data: RcePrices | None, now: datetime) -> None:
        if rce_data is None:
            self.max_upcoming_peak = None
            return
        self.max_upcoming_peak = max_upcoming_peak(rce_data, now)


def _hourly_slots(
    day_prices: RceDayPrices | None,
) -> list[tuple[float, datetime]]:
    """Build (price, datetime) pairs from RceDayPrices."""
    if day_prices is None or not day_prices.hour_price:
        return []
    return [
        (price, day_prices.datetime_at_hour(hour))
        for hour, price in enumerate(day_prices.hour_price)
    ]


def max_upcoming_peak(rce_data: RcePrices, now: datetime) -> UpcomingPeak | None:
    """Max RCE price w nadchodzącym peak window — z time-of-day branching.

    - **Do 12:00**: dzisiejszy poranny peak (today 5-12).
      Use case: rano user widzi czy poranny peak już był / będzie.
    - **Od 12:00**: dzisiejszy wieczorny + jutrzejszy poranny/popołudniowy
      (today 16-24 + tomorrow 6-14). Standard "next peak" decision dla
      afternoon-static, evening discharge etc.

      Wieczór zaczyna się o 16:00, nie o 19:00: zimowe T2 rusza o 16:00,
      a `EveningPlan` w dni wolne i tak przeszukuje 16-23. Okno jest tu
      szersze niż strefa taryfowa i to jest w porządku — pytamy o cenę,
      nie o strefę, a wynik i tak przechodzi przez `max()`.

    Sensor **NIE filtruje past slots** — intentional, dla retrospekcji
    (rano user chce widzieć czy oddawaliśmy w wieczornym peaku, sprawdza
    po południu czy poranny peak był high).

    Tie-break: późniejsza godzina (dłużej akumulujemy energię w baterii).
    Returns None gdy brak danych dla aktywnego window.
    """
    today_slots = _hourly_slots(rce_data.today)
    candidates: list[tuple[float, datetime]] = []
    if now.hour < 12:
        # Morning cycle: dzisiejszy peak rano (5-12)
        candidates.extend(s for s in today_slots if 5 <= s[1].hour < 12)
    else:
        # Afternoon/evening cycle: dziś wieczór + jutro morning/afternoon
        candidates.extend(s for s in today_slots if 16 <= s[1].hour < 24)
        candidates.extend(
            s for s in _hourly_slots(rce_data.tomorrow) if 6 <= s[1].hour < 14
        )
    if not candidates:
        return None
    best = max(candidates, key=lambda x: (x[0], x[1]))
    return UpcomingPeak(price=best[0], datetime=best[1])
