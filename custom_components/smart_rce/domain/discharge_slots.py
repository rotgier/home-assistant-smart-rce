"""DischargeSlots — algorytm peaków RCE + cached state.

Aggregate dla decyzji "kiedy rozładować baterię w peak ceny".
Trzyma cached fields (`max_upcoming_peak`, `best_morning_discharge_slot`)
recomputowane przy `update_rce` i `update_hourly` w `Ems`.

Sensor.py czyta jako field (`ems.discharge_slots.max_upcoming_peak`),
nie metodę — zero domain-specific arguments wyciekających do warstwy
ekspozycji.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from ..const import GROSS_MULTIPLIER
from .rce import RceData

# Morning discharge window — szukamy peak ceny rano przed startem PV.
# Use case: niedzielny weekend morning, gdy RCE peak rano > niska niedzielna
# cena dzienna. Bateria pełna (lub częściowo) z nocy → discharge do 10% w
# peak hour, niedzielny PV potem napełnia. Pure profit: konwersja
# "PV-do-grid-za-0" → "discharge-za-peak". Zero-risk decision (bateria
# rano = realny stan, nie zgadujemy z forecast).
MORNING_DISCHARGE_START_HOUR: Final[int] = 5  # tomorrow, inclusive
MORNING_DISCHARGE_END_HOUR: Final[int] = 8  # tomorrow, exclusive (czyli 5,6,7)

# Tolerancja near-peak dla tie-break w best_morning_discharge_slot.
# Sloty z ceną ≤ tolerance od peaku traktujemy jako "remis" — wybieramy
# najpóźniejszy z near-peak slotów (skraca czas trzymania pustej baterii
# do startu PV). Stała wyrażona w **brutto** (myślenie konsumenckie),
# konwertowana do netto przy porównaniu (RceData.prices są w netto).
# 20 zł/MWh brutto ≈ 2 grosze/kWh brutto.
MORNING_DISCHARGE_TIE_BREAK_TOLERANCE_PLN_MWH_GROSS: Final[float] = 20.0


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
    best_morning_discharge_slot: UpcomingPeak | None = None

    def update(self, rce_data: RceData | None, now: datetime) -> None:
        if rce_data is None:
            self.max_upcoming_peak = None
            self.best_morning_discharge_slot = None
            return
        self.max_upcoming_peak = max_upcoming_peak(rce_data, now)
        self.best_morning_discharge_slot = best_morning_discharge_slot(rce_data, now)


def max_upcoming_peak(rce_data: RceData, now: datetime) -> UpcomingPeak | None:
    """Max RCE price w nadchodzącym peak window — z time-of-day branching.

    - **Do 12:00**: dzisiejszy poranny peak (today.prices 5-12).
      Use case: rano user widzi czy poranny peak już był / będzie.
    - **Od 12:00**: dzisiejszy wieczorny + jutrzejszy poranny/popołudniowy
      (today 19-24 + tomorrow 6-14). Standard "next peak" decision dla
      afternoon-static, evening discharge etc.

    Sensor **NIE filtruje past slots** — intentional, dla retrospekcji
    (rano user chce widzieć czy oddawaliśmy w wieczornym peaku, sprawdza
    po południu czy poranny peak był high).

    Tie-break: późniejsza godzina (dłużej akumulujemy energię w baterii).
    Returns None gdy brak danych dla aktywnego window.
    """
    candidates: list[tuple[float, datetime]] = []
    if now.hour < 12:
        # Morning cycle: dzisiejszy peak rano (5-12)
        candidates.extend(
            (p["price"], p["datetime"])
            for p in (rce_data.today.prices if rce_data.today else [])
            if 5 <= p["datetime"].hour < 12
        )
    else:
        # Afternoon/evening cycle: dziś wieczór + jutro morning/afternoon
        candidates.extend(
            (p["price"], p["datetime"])
            for p in (rce_data.today.prices if rce_data.today else [])
            if 19 <= p["datetime"].hour < 24
        )
        candidates.extend(
            (p["price"], p["datetime"])
            for p in (rce_data.tomorrow.prices if rce_data.tomorrow else [])
            if 6 <= p["datetime"].hour < 14
        )
    if not candidates:
        return None
    best = max(candidates, key=lambda x: (x[0], x[1]))
    return UpcomingPeak(price=best[0], datetime=best[1])


def best_morning_discharge_slot(
    rce_data: RceData, now: datetime
) -> UpcomingPeak | None:
    """Max RCE w nadchodzących godzinach rano [5, 8) — peak przed startem PV.

    Patrzy w **today AND tomorrow**: filter `dt > now` zostawia tylko
    future slots. Po północy `tomorrow=None` (przed publikacją RCE jutra)
    ale `today` już ma future slots 5-8 → fallback działa.

    Tie-break z tolerancją: sloty z ceną w odległości
    ≤ MORNING_DISCHARGE_TIE_BREAK_TOLERANCE (~2 gr/kWh brutto) od peaku
    są równoważne — wybieramy najpóźniejszy (krótszy czas trzymania
    pustej baterii do startu PV).

    Returns None gdy brak future slots w range.
    """
    candidates: list[tuple[float, datetime]] = []
    for day in (rce_data.today, rce_data.tomorrow):
        if not day:
            continue
        candidates.extend(
            (p["price"], p["datetime"])
            for p in day.prices
            if MORNING_DISCHARGE_START_HOUR
            <= p["datetime"].hour
            < MORNING_DISCHARGE_END_HOUR
            and p["datetime"] > now
        )
    if not candidates:
        return None
    max_price = max(c[0] for c in candidates)
    tolerance_net = (
        MORNING_DISCHARGE_TIE_BREAK_TOLERANCE_PLN_MWH_GROSS / GROSS_MULTIPLIER
    )
    near_peak = [c for c in candidates if c[0] >= max_price - tolerance_net]
    best = max(near_peak, key=lambda x: x[1])
    return UpcomingPeak(price=best[0], datetime=best[1])
