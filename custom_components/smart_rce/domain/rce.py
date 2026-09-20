"""Domain logic of RCE prices."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import re
from typing import Any, Final
from zoneinfo import ZoneInfo

from ..const import GROSS_MULTIPLIER

TIMEZONE: Final = ZoneInfo("Europe/Warsaw")


_DST_MARKER = re.compile(r"(\d{2})[ab](:)")


@dataclass
class RceDayPrices:
    """RCE prices of given day — hour-indexed tuple + day."""

    published_at: datetime | None
    day: date
    hour_price: tuple[float, ...]  # length matches available hours, indexed by hour

    @property
    def avg_price(self) -> float | None:
        """Average price for the day (rounded to 2 decimals). None for empty hour_price."""
        if not self.hour_price:
            return None
        return round(sum(self.hour_price) / len(self.hour_price), 2)

    def datetime_at_hour(self, hour: int) -> datetime:
        return datetime.combine(self.day, time(hour, 0), TIMEZONE)

    def gross_at(self, hour: int) -> float:
        """Gross PLN/MWh for `hour`; 0.0 when the day is that short.

        Gross is what the user reasons in and what compares directly with the
        grid purchase costs in `tariff`, so decisions are made in this unit
        rather than converting at each call site.
        """
        if hour >= len(self.hour_price):
            return 0.0
        return self.hour_price[hour] * GROSS_MULTIPLIER

    def max_gross_in(self, hours: Iterable[int]) -> float:
        """Best gross price among `hours`; 0.0 when none of them exist."""
        return max((self.gross_at(hour) for hour in hours), default=0.0)

    def hours_clearing(
        self, hours: Iterable[int], *, at_least: float, above: float = 0.0
    ) -> list[int]:
        """Hours priced at or over `at_least` AND strictly over `above`.

        Two bars rather than one: `at_least` is an absolute floor, `above` a
        rival price that must be beaten outright (a tie is no reason to act).
        """
        return [
            hour
            for hour in hours
            if (price := self.gross_at(hour)) >= at_least and price > above
        ]

    @classmethod
    def create_from_json(cls, data: dict[str, Any]) -> RceDayPrices | None:
        """Parse RCE api data into domain object.

        API returns 15-minute intervals. We aggregate to hourly averages,
        clamping negative prices to 0 before averaging.
        """
        hourly_groups: dict[datetime, list[float]] = {}
        published_at = None

        for record in data["value"]:
            published_at = record["publication_ts"]
            dtime = _parse_dtime(record["dtime"])
            dtime = dtime.replace(tzinfo=TIMEZONE)
            interval_start = dtime - timedelta(minutes=15)
            hour_key = interval_start.replace(minute=0, second=0)
            hourly_groups.setdefault(hour_key, []).append(record["rce_pln"])

        if published_at is None or not hourly_groups:
            return None

        sorted_keys = sorted(hourly_groups)
        day = sorted_keys[0].date()
        hour_price = tuple(
            round(sum(max(0.0, p) for p in hourly_groups[k]) / len(hourly_groups[k]), 2)
            for k in sorted_keys
        )
        return cls(published_at=published_at, day=day, hour_price=hour_price)

    @classmethod
    def from_sensor_attr(cls, prices_attr: list[dict]) -> RceDayPrices | None:
        """Build RceDayPrices z restored sensor attributes."""
        if not prices_attr:
            return None
        parsed = sorted(
            ((datetime.fromisoformat(p["datetime"]), p["price"]) for p in prices_attr),
            key=lambda x: x[0],
        )
        if not parsed:
            return None
        return cls(
            published_at=None,
            day=parsed[0][0].date(),
            hour_price=tuple(price for _, price in parsed),
        )


@dataclass(frozen=True, kw_only=True)
class RcePrices:
    """RCE prices data — two-day snapshot from API or restored cache."""

    fetched_at: datetime
    today: RceDayPrices | None = None
    tomorrow: RceDayPrices | None = None


def _parse_dtime(text: str) -> datetime:
    """Parse a PSE timestamp, including the one day a year that is not ISO.

    On the autumn clock change the repeated hour is marked `02a:15:00`, which
    `fromisoformat` refuses outright — so without this the price fetch raises on
    that day. For RCE it would leave EMS without prices; for the deposit it is
    worse, because the day can never be valued and the fetch watermark would
    stop there for good.

    Both copies of the hour fold into the same hourly average, which is the same
    approximation `RcePriceReader` already documents: a few groszy, twice a year.
    """
    return datetime.fromisoformat(_DST_MARKER.sub(r"\1\2", text))
