"""Domain logic of RCE prices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from zoneinfo import ZoneInfo

TIMEZONE: Final = ZoneInfo("Europe/Warsaw")

# Morning discharge window — szukamy peak ceny rano przed startem PV.
# Use case: niedzielny weekend morning, gdy RCE peak rano > niska niedzielna
# cena dzienna. Bateria pełna (lub częściowo) z nocy → discharge do 10% w
# peak hour, niedzielny PV potem napełnia. Pure profit: konwersja
# "PV-do-grid-za-0" → "discharge-za-peak". Zero-risk decision (bateria
# rano = realny stan, nie zgadujemy z forecast).
MORNING_DISCHARGE_START_HOUR: Final[int] = 5  # tomorrow, inclusive
MORNING_DISCHARGE_END_HOUR: Final[int] = 8  # tomorrow, exclusive (czyli 5,6,7)


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

    def max_upcoming_peak(self) -> UpcomingPeak | None:
        """Max RCE price w evening today (19-22) + morning tomorrow (6-9).

        Returns None gdy brak danych dla obu okresów. Jeśli kilka godzin ma
        tę samą max cenę, zwraca **najpóźniejszą** — dłużej akumulujemy
        energię w baterii (PV w popołudnie wciąż coś dostarcza), buffer
        czasowy większy.
        """
        candidates: list[tuple[float, datetime]] = [
            (p["price"], p["datetime"])
            for p in (self.today.prices if self.today else [])
            if 19 <= p["datetime"].hour < 22
        ]
        candidates.extend(
            (p["price"], p["datetime"])
            for p in (self.tomorrow.prices if self.tomorrow else [])
            if 6 <= p["datetime"].hour < 9
        )
        if not candidates:
            return None
        # max po (price, datetime) — przy remisie cenowym wybierze późniejszą
        best = max(candidates, key=lambda x: (x[0], x[1]))
        return UpcomingPeak(price=best[0], datetime=best[1])

    def best_morning_discharge_slot(self, now: datetime) -> UpcomingPeak | None:
        """Max RCE w godzinach jutra rano [5, 8) — peak przed startem PV.

        Filter: dt > now (jeśli już po peak rano dziś, sensor patrzy w jutro).
        Tie-break: późniejsza godzina (preferuje slot bliżej startu PV,
        krótszy czas trzymania pustej baterii).
        Returns None gdy brak future slots w range (np. po 8:00 dziś gdy nie
        ma jeszcze cen jutra, lub gdy tomorrow=None przed publikacją RCE).
        """
        if not self.tomorrow:
            return None
        candidates: list[tuple[float, datetime]] = [
            (p["price"], p["datetime"])
            for p in self.tomorrow.prices
            if MORNING_DISCHARGE_START_HOUR
            <= p["datetime"].hour
            < MORNING_DISCHARGE_END_HOUR
            and p["datetime"] > now
        ]
        if not candidates:
            return None
        best = max(candidates, key=lambda x: (x[0], x[1]))
        return UpcomingPeak(price=best[0], datetime=best[1])
