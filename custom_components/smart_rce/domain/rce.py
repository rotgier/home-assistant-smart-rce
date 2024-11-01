"""Domain logic of RCE prices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from zoneinfo import ZoneInfo

TIMEZONE: Final = ZoneInfo("Europe/Warsaw")


@dataclass
class RceDayPrices:
    """RCE prices of given day."""

    published_at: datetime
    prices: list[dict[str, float | datetime]]

    @classmethod
    def create_from_json(cls, data) -> RceDayPrices | None:
        """Parse RCE api data into domain object."""

        prices = []
        published_at = None
        for price in data["value"]:
            published_at = price["source_datetime"]
            date = price["doba"]
            hour = price["udtczas_oreb"][:5]
            date_hour = datetime.fromisoformat(f"{date} {hour}")
            date_hour = date_hour.replace(tzinfo=TIMEZONE)
            if date_hour.minute == 0:
                prices.append(
                    {
                        "datetime": date_hour.replace(minute=0),
                        "price": price["rce_pln"],
                    }
                )

        return cls(published_at, prices) if published_at else None


@dataclass(frozen=True, kw_only=True)
class RceData:
    """RCE prices data."""

    fetched_at: datetime
    today: RceDayPrices
    tomorrow: RceDayPrices
