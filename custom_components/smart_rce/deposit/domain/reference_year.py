"""ReferenceYear — the twelve-month profile that projections repeat forward.

Deliberately derived from realised data rather than configured: it is the last
twelve closed months of production and consumption, so every downstream number
(capacity, winter outlook, expiry year) tracks how the household actually
behaves instead of a hardcoded assumption.

Caveat worth remembering when reading a projection: months that predate a
strategy change still carry the old behaviour, so they understate what the
current strategy will earn. The current, partially elapsed month can be handed
in separately (`partial`) to override its calendar slot with fresher data.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace

from .billing_month import BillingMonth
from .tariff import Zone

_MONTHS_IN_YEAR = 12


@dataclass(frozen=True)
class ReferenceYear:
    """Exactly twelve records, one per calendar month."""

    records: tuple[MonthRecord, ...]

    @classmethod
    def from_history(
        cls, history: Sequence[MonthRecord], *, partial: MonthRecord | None = None
    ) -> ReferenceYear:
        """Take the twelve months ending with the newest record in `history`."""
        by_month = {record.month: record for record in history}
        if not by_month:
            raise ValueError("ReferenceYear needs at least twelve months of history")
        picked: dict[int, MonthRecord] = {}
        cursor = max(by_month)
        for _ in range(_MONTHS_IN_YEAR):
            record = by_month.get(cursor)
            if record is None:
                raise ValueError(f"ReferenceYear: missing history for {cursor}")
            picked[cursor.month] = record
            cursor = cursor.previous()
        if partial is not None:
            picked[partial.month.month] = partial
        return cls(tuple(picked[number] for number in sorted(picked)))

    def for_month(self, month: BillingMonth) -> MonthRecord:
        """Return the reference record whose calendar month matches `month`."""
        return self.records[month.month - 1]

    def __iter__(self) -> Iterator[MonthRecord]:
        return iter(self.records)


@dataclass(frozen=True)
class MonthRecord:
    """One month of realised volumes and the deposit they earned."""

    month: BillingMonth
    exported_kwh: float
    deposit_earned: float
    import_kwh: Mapping[Zone, float]

    def extrapolated(self, elapsed_days: int) -> MonthRecord:
        """Scale a partially elapsed month up to its full calendar length.

        Linear on days: good enough for a month-ahead view, and the alternative
        (waiting for the month to close) would leave the freshest month out of
        the profile entirely.
        """
        if elapsed_days <= 0:
            raise ValueError("elapsed_days must be positive")
        factor = self.month.days / elapsed_days
        return self.scaled(factor)

    def scaled(self, factor: float) -> MonthRecord:
        return replace(
            self,
            exported_kwh=self.exported_kwh * factor,
            deposit_earned=self.deposit_earned * factor,
            import_kwh={zone: kwh * factor for zone, kwh in self.import_kwh.items()},
        )

    @property
    def total_import_kwh(self) -> float:
        return sum(self.import_kwh.values())
