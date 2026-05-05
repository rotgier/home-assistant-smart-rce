"""Domain logic of RCE prices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Final

from zoneinfo import ZoneInfo

TIMEZONE: Final = ZoneInfo("Europe/Warsaw")


@dataclass
class RceDayPrices:
    """RCE prices of given day — hour-indexed tuple + day."""

    published_at: datetime | None
    day: date
    hour_price: tuple[float, ...]  # length matches available hours, indexed by hour

    def datetime_at_hour(self, hour: int) -> datetime:
        return datetime.combine(self.day, time(hour, 0), TIMEZONE)

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
    """RCE prices data."""

    fetched_at: datetime
    today: RceDayPrices
    tomorrow: RceDayPrices
