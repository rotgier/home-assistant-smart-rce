"""Domain logic of RCE prices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from zoneinfo import ZoneInfo

from ..const import GROSS_MULTIPLIER

TIMEZONE: Final = ZoneInfo("Europe/Warsaw")

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


@dataclass
class RceDayPrices:
    """RCE prices of given day."""

    published_at: datetime
    prices: list[dict[str, float | datetime]]

    @classmethod
    def create_from_json(cls, data) -> RceDayPrices | None:
        """Parse RCE api data into domain object.

        API returns 15-minute intervals. We aggregate to hourly averages,
        clamping negative prices to 0 before averaging.
        """
        hourly_groups: dict[datetime, list[float]] = {}
        published_at = None

        for record in data["value"]:
            published_at = record["publication_ts"]
            dtime = datetime.fromisoformat(record["dtime"])
            dtime = dtime.replace(tzinfo=TIMEZONE)
            interval_start = dtime - timedelta(minutes=15)
            hour_key = interval_start.replace(minute=0, second=0)
            hourly_groups.setdefault(hour_key, []).append(record["rce_pln"])

        prices = []
        for hour_key in sorted(hourly_groups):
            raw_prices = hourly_groups[hour_key]
            clamped = [max(0, p) for p in raw_prices]
            avg_price = sum(clamped) / len(clamped)
            prices.append({"datetime": hour_key, "price": round(avg_price, 2)})

        return cls(published_at, prices) if published_at else None


@dataclass(frozen=True, kw_only=True)
class UpcomingPeak:
    """Najwyższa cena RCE w nadchodzących drogich oknach.

    Wieczór dziś (19-22) lub rano jutro (6-9). Zwracane raw rce_pln
    (PLN/MWh, netto) — konwersja brutto odbywa się w warstwie sensorów.
    """

    price: float
    datetime: datetime


@dataclass(frozen=True, kw_only=True)
class RceData:
    """RCE prices data."""

    fetched_at: datetime
    today: RceDayPrices
    tomorrow: RceDayPrices

    def max_upcoming_peak(self, now: datetime) -> UpcomingPeak | None:
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
                for p in (self.today.prices if self.today else [])
                if 5 <= p["datetime"].hour < 12
            )
        else:
            # Afternoon/evening cycle: dziś wieczór + jutro morning/afternoon
            candidates.extend(
                (p["price"], p["datetime"])
                for p in (self.today.prices if self.today else [])
                if 19 <= p["datetime"].hour < 24
            )
            candidates.extend(
                (p["price"], p["datetime"])
                for p in (self.tomorrow.prices if self.tomorrow else [])
                if 6 <= p["datetime"].hour < 14
            )
        if not candidates:
            return None
        # max po (price, datetime) — przy remisie cenowym wybierze późniejszą
        best = max(candidates, key=lambda x: (x[0], x[1]))
        return UpcomingPeak(price=best[0], datetime=best[1])

    def best_morning_discharge_slot(self, now: datetime) -> UpcomingPeak | None:
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
        for day in (self.today, self.tomorrow):
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
