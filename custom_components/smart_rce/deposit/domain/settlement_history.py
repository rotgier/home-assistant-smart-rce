"""SettlementHistory — closed months plus the days of the month in progress.

The ledger settles whole months, but data arrives daily, so this aggregate holds
both: `months` for what is finished and `days` for what is accumulating.

A month rolls up only once **no day of it can still be re-read** — a week after it
ends, matching the window the refresh re-reads. Rolling up the moment the next
month started, which is what this did until 2026-09-04, made the following week's
re-reads land as a *second* record for a month that was already closed: August was
counted once in full and once again for its last five days.

`last_data_day` is the fetch watermark: the refresh asks the meter for everything
after it, so a run that fails simply leaves more to do next time.

`last_meter_call` is a different thing and worth keeping apart: when the meter
last answered. The refresh re-reads the recent past on every run, so without it a
burst of reloads would be a burst of logins — and TAURON bans on those for half a
day. It is a moment rather than a date because the run may legitimately try again
later the same day, when yesterday was not ready at the first attempt.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import datetime
from typing import TYPE_CHECKING, Any

from .billing_month import BillingMonth
from .reference_year import MonthRecord
from .tariff import Zone

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# A month stays open to correction for as long as the refresh still re-reads its
# days. Must not be shorter than `DepositRefreshService._TRAILING_DAYS`, or a
# re-read would arrive for a month that has already been settled.
_FINALISED_AFTER = datetime.timedelta(days=7)


class SettlementHistory:
    """Everything the deposit context knows about realised production and import."""

    def __init__(
        self,
        months: Iterable[MonthRecord],
        days: Iterable[DayRecord] = (),
        last_data_day: datetime.date | None = None,
        last_meter_call: datetime.datetime | None = None,
    ) -> None:
        self._months = sorted(months, key=lambda record: record.month)
        self._days = sorted(days, key=lambda record: record.day)
        self._last_data_day = last_data_day
        self._last_meter_call = last_meter_call

    @classmethod
    def from_seed(cls, months: Iterable[MonthRecord]) -> SettlementHistory:
        """Bootstrap from the shipped seed — closed months only.

        The seed's partial month is deliberately dropped: it carries no per-day
        detail, so the first refresh re-fetches that month from its first day and
        replaces an extrapolation with measurements.
        """
        settled = sorted(months, key=lambda record: record.month)
        if not settled:
            raise ValueError("seed history is empty")
        return cls(settled, last_data_day=_last_day_of(settled[-1].month))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SettlementHistory:
        watermark = data.get("last_data_day")
        called = data.get("last_meter_call")
        return cls(
            months=[_month_from_dict(row) for row in data.get("months", ())],
            days=[_day_from_dict(row) for row in data.get("days", ())],
            last_data_day=datetime.date.fromisoformat(watermark) if watermark else None,
            last_meter_call=_parse_moment(called),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "months": [_month_to_dict(record) for record in self._months],
            "days": [_day_to_dict(record) for record in self._days],
            "last_data_day": self._last_data_day.isoformat()
            if self._last_data_day
            else None,
            "last_meter_call": self._last_meter_call.isoformat()
            if self._last_meter_call
            else None,
        }

    @property
    def months(self) -> list[MonthRecord]:
        """Closed months, oldest first."""
        return list(self._months)

    @property
    def last_data_day(self) -> datetime.date | None:
        return self._last_data_day

    @property
    def last_meter_call(self) -> datetime.datetime | None:
        """When the meter last answered — the refresh rations itself on this."""
        return self._last_meter_call

    def mark_meter_called(self, moment: datetime.datetime) -> None:
        """Record a successful meter call.

        Only successful ones: a failed fetch must stay retryable, or a single
        outage would cost a whole day of data.
        """
        self._last_meter_call = moment

    @property
    def elapsed_days(self) -> int:
        """Days measured in the month in progress."""
        return len(self._current_month_days())

    @property
    def unsettled_days(self) -> tuple[datetime.date, ...]:
        """Stored days that look like the meter had not finished them.

        A day of zero import across all twenty-four hours is not a quiet day, it
        is a day TAURON listed before it balanced it. Nothing should be able to
        store one any more (`is_balanced` refuses), so this is a watch on the
        store itself — and the alarm that was missing when four such days sat
        here unnoticed for a week.

        Only the month in progress: once a month rolls up, its days are gone and
        the figure is settled anyway.
        """
        return tuple(day.day for day in self._days if day.total_import_kwh <= 0)

    @property
    def partial(self) -> MonthRecord | None:
        """The month in progress, summed over measured days (not extrapolated).

        Only the newest month: `days` also holds the tail of the previous one
        until it finalises, and summing both would invent a monster month.
        """
        days = self._current_month_days()
        if not days:
            return None
        return _sum_days(BillingMonth(days[0].day.year, days[0].day.month), days)

    def _current_month_days(self) -> list[DayRecord]:
        if not self._days:
            return []
        newest = self._days[-1].day
        return [
            record
            for record in self._days
            if (record.day.year, record.day.month) == (newest.year, newest.month)
        ]

    def next_day_to_fetch(self) -> datetime.date | None:
        """First day not yet measured, or None when nothing was ever fetched."""
        if self._last_data_day is None:
            return None
        return self._last_data_day + datetime.timedelta(days=1)

    def add_days(self, records: Iterable[DayRecord]) -> None:
        """Merge measured days (re-fetching a day replaces it), then roll up."""
        merged = {record.day: record for record in self._days}
        merged.update({record.day: record for record in records})
        self._days = sorted(merged.values(), key=lambda record: record.day)
        if self._days:
            newest = self._days[-1].day
            self._last_data_day = max(self._last_data_day or newest, newest)
        self._roll_up_finished_months()

    def _roll_up_finished_months(self) -> None:
        """Settle every month that can no longer change, and drop its days."""
        if not self._days:
            return
        newest = self._days[-1].day
        settled = {record.month for record in self._months}
        grouped: dict[BillingMonth, list[DayRecord]] = defaultdict(list)
        for record in self._days:
            grouped[BillingMonth(record.day.year, record.day.month)].append(record)
        keep: list[DayRecord] = []
        for month, records in sorted(grouped.items()):
            if month in settled:
                continue  # already a closed record; its days cannot improve it
            if _last_day_of(month) > newest - _FINALISED_AFTER:
                keep.extend(records)
            else:
                self._months.append(_sum_days(month, records))
        self._months.sort(key=lambda record: record.month)
        self._days = keep


@dataclass(frozen=True)
class DayRecord:
    """One measured day: what went out, what it earned, what came in per zone."""

    day: datetime.date
    exported_kwh: float
    deposit_earned: float
    import_kwh: Mapping[Zone, float]

    @property
    def total_import_kwh(self) -> float:
        return sum(self.import_kwh.values())


def _sum_days(month: BillingMonth, records: list[DayRecord]) -> MonthRecord:
    imports: dict[Zone, float] = dict.fromkeys(Zone, 0.0)
    for record in records:
        for zone, kwh in record.import_kwh.items():
            imports[zone] += kwh
    return MonthRecord(
        month=month,
        exported_kwh=sum(record.exported_kwh for record in records),
        deposit_earned=sum(record.deposit_earned for record in records),
        import_kwh=imports,
    )


def _parse_moment(text: str | None) -> datetime.datetime | None:
    """Read the mark, tolerating the plain date an older version wrote."""
    if not text:
        return None
    parsed = datetime.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def _last_day_of(month: BillingMonth) -> datetime.date:
    return datetime.date(month.year, month.month, month.days)


def _month_to_dict(record: MonthRecord) -> dict[str, Any]:
    return {
        "month": str(record.month),
        "exported_kwh": round(record.exported_kwh, 3),
        "deposit_earned": round(record.deposit_earned, 2),
        "import_kwh": {zone.value: round(record.import_kwh[zone], 3) for zone in Zone},
    }


def _month_from_dict(row: dict[str, Any]) -> MonthRecord:
    return MonthRecord(
        month=BillingMonth.parse(row["month"]),
        exported_kwh=row["exported_kwh"],
        deposit_earned=row["deposit_earned"],
        import_kwh={zone: row["import_kwh"][zone.value] for zone in Zone},
    )


def _day_to_dict(record: DayRecord) -> dict[str, Any]:
    return {
        "day": record.day.isoformat(),
        "exported_kwh": round(record.exported_kwh, 3),
        "deposit_earned": round(record.deposit_earned, 2),
        "import_kwh": {zone.value: round(record.import_kwh[zone], 3) for zone in Zone},
    }


def _day_from_dict(row: dict[str, Any]) -> DayRecord:
    return DayRecord(
        day=datetime.date.fromisoformat(row["day"]),
        exported_kwh=row["exported_kwh"],
        deposit_earned=row["deposit_earned"],
        import_kwh={zone: row["import_kwh"][zone.value] for zone in Zone},
    )
