"""The hourly detail behind a settled day, kept so a day can be re-read locally.

The ledger only needs one number per day, and for a long time that was all we
stored. It turned out to be one number too few: the hourly shape is what answers
"at which hour do we actually export", which is the question the whole export
strategy rests on, and without the prices a settled day cannot be re-valued
without asking TAURON again — the one source that bans on being asked too often.

Kept per calendar month because that is already the aggregate boundary here, and
because it makes a closed month a file nothing writes to any more. Roughly 20 kB
a month, so the whole history is smaller than one photo.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from .billing_month import BillingMonth


@dataclass(frozen=True)
class PricedHour:
    """One metered hour and what the market paid for it.

    The price rides along rather than being re-fetched because it is the half
    that makes the pair re-valuable: PSE publishes corrections, tariffs change,
    and neither should mean another login to the meter.
    """

    exported_kwh: float
    imported_kwh: float
    price_pln_mwh: float | None
    """None for hours PSE never published a price for — before July 2024, mostly."""


class MonthlyHours:
    """Every measured hour of one billing month."""

    def __init__(
        self,
        month: BillingMonth,
        days: Mapping[datetime.date, Mapping[int, PricedHour]] | None = None,
    ) -> None:
        self._month = month
        self._days: dict[datetime.date, dict[int, PricedHour]] = {
            day: dict(hours) for day, hours in (days or {}).items()
        }

    @classmethod
    def from_dict(cls, month: BillingMonth, data: Mapping[str, Any]) -> MonthlyHours:
        return cls(
            month,
            {
                datetime.date.fromisoformat(day): {
                    int(hour): PricedHour(*values) for hour, values in hours.items()
                }
                for day, hours in data.get("days", {}).items()
            },
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise compactly — an hour is three numbers, not three field names."""
        return {
            "month": str(self._month),
            "days": {
                day.isoformat(): {
                    str(hour): [
                        hours[hour].exported_kwh,
                        hours[hour].imported_kwh,
                        hours[hour].price_pln_mwh,
                    ]
                    for hour in sorted(hours)
                }
                for day, hours in sorted(self._days.items())
            },
        }

    @property
    def month(self) -> BillingMonth:
        return self._month

    @property
    def days(self) -> dict[datetime.date, dict[int, PricedHour]]:
        return {day: dict(hours) for day, hours in sorted(self._days.items())}

    def record(self, day: datetime.date, hours: Mapping[int, PricedHour]) -> None:
        """Store a day, replacing whatever was there — a re-read is the newer truth."""
        if day.year != self._month.year or day.month != self._month.month:
            raise ValueError(f"{day} does not belong to {self._month}")
        self._days[day] = dict(hours)

    def __len__(self) -> int:
        return len(self._days)

    def __iter__(self) -> Iterator[datetime.date]:
        return iter(sorted(self._days))
