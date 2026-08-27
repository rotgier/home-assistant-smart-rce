"""SettlementHistory — closed months plus the days of the month in progress.

The ledger settles whole months, but data arrives daily, so this aggregate holds
both: `months` for what is finished and `days` for what is accumulating. A month
rolls up as soon as a day from a later month arrives, which also makes a missed
run self-healing — nothing is lost, it just rolls up later.

`last_data_day` is the fetch watermark: the refresh asks the meter for everything
after it, so a run that fails simply leaves more to do next time.

`last_meter_call` is a different thing and worth keeping apart: the day we last
got an answer out of the meter. The refresh re-reads the recent past on every
run, so without it a burst of reloads would be a burst of logins — and TAURON
bans on those for half a day.
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


class SettlementHistory:
    """Everything the deposit context knows about realised production and import."""

    def __init__(
        self,
        months: Iterable[MonthRecord],
        days: Iterable[DayRecord] = (),
        last_data_day: datetime.date | None = None,
        last_meter_call: datetime.date | None = None,
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
            last_meter_call=datetime.date.fromisoformat(called) if called else None,
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
    def last_meter_call(self) -> datetime.date | None:
        """Day the meter last answered — the refresh rations itself on this."""
        return self._last_meter_call

    def mark_meter_called(self, day: datetime.date) -> None:
        """Record a successful meter call.

        Only successful ones: a failed fetch must stay retryable, or a single
        outage at 04:15 would cost a whole day of data.
        """
        self._last_meter_call = day

    @property
    def elapsed_days(self) -> int:
        """Days measured in the month in progress."""
        return len(self._days)

    @property
    def partial(self) -> MonthRecord | None:
        """The month in progress, summed over measured days (not extrapolated)."""
        if not self._days:
            return None
        first = self._days[0].day
        return _sum_days(BillingMonth(first.year, first.month), self._days)

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
        """Move days of any month older than the newest day's month into `months`."""
        if not self._days:
            return
        newest = self._days[-1].day
        current = BillingMonth(newest.year, newest.month)
        grouped: dict[BillingMonth, list[DayRecord]] = defaultdict(list)
        for record in self._days:
            grouped[BillingMonth(record.day.year, record.day.month)].append(record)
        keep: list[DayRecord] = []
        for month, records in sorted(grouped.items()):
            if month < current:
                self._months.append(_sum_days(month, records))
            else:
                keep.extend(records)
        self._months.sort(key=lambda record: record.month)
        self._days = keep


@dataclass(frozen=True)
class DayRecord:
    """One measured day: what went out, what it earned, what came in per zone."""

    day: datetime.date
    exported_kwh: float
    deposit_earned: float
    import_kwh: Mapping[Zone, float]


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
