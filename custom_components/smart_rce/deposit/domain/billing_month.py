"""BillingMonth — the unit of net-billing settlement."""

from __future__ import annotations

import calendar
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class BillingMonth:
    """A settlement month. Ordered, so ranges and comparisons read naturally."""

    year: int
    month: int

    @classmethod
    def parse(cls, text: str) -> BillingMonth:
        """Parse `YYYY-MM` — the form used by the CSV seed and by storage."""
        return cls(int(text[:4]), int(text[5:7]))

    def next(self) -> BillingMonth:
        return self.shifted(1)

    def previous(self) -> BillingMonth:
        return self.shifted(-1)

    def shifted(self, months: int) -> BillingMonth:
        total = self.year * 12 + (self.month - 1) + months
        return BillingMonth(total // 12, total % 12 + 1)

    def months_since(self, other: BillingMonth) -> int:
        """Age in months relative to `other` (negative when this one is earlier)."""
        return self._ordinal - other._ordinal

    @property
    def _ordinal(self) -> int:
        return self.year * 12 + self.month

    @property
    def days(self) -> int:
        """Calendar length — used to extrapolate a partially elapsed month."""
        return calendar.monthrange(self.year, self.month)[1]

    def __str__(self) -> str:
        return f"{self.year}-{self.month:02d}"
