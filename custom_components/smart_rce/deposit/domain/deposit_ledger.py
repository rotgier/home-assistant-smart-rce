"""DepositLedger — the prosumer deposit account (aggregate).

Settlement rules (SSOT: `fotowoltaika/tauron_faktury/ZASADY_ROZLICZENIA.md`,
confirmed with TAURON):

- Deposit earned in month M is credited to the account and becomes spendable
  from M+1. That lag is the binding constraint in spring: March's bill is paid
  with February's balance, not with March's own production.
- It offsets **active energy only**; distribution is always cash.
- A tranche is valid 12 months. What is left when it expires is refunded in
  cash at 30% (hourly RCE settlement with the 1.23 factor, our regime since
  02.2025) or 20% (earlier RCEm / RCE x1.0 regime). The rest is forfeited.
- Consumption is FIFO — oldest tranche first, which is what keeps a permanently
  positive balance from ageing out as long as it stays below one year of bills.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from .billing_month import BillingMonth


class DepositLedger:
    """FIFO queue of deposit tranches, one per month earned."""

    VALIDITY_MONTHS: Final = 12
    _HOURLY_SETTLEMENT_FROM: Final = BillingMonth(2025, 2)
    _REFUND_SHARE_HOURLY: Final = 0.30
    _REFUND_SHARE_MONTHLY: Final = 0.20
    _EPSILON: Final = 1e-9

    def __init__(self, tranches: Iterable[tuple[BillingMonth, float]] = ()) -> None:
        self._tranches = [_Tranche(month, amount) for month, amount in tranches]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DepositLedger:
        return cls(
            (BillingMonth.parse(row["month"]), float(row["remaining"]))
            for row in data.get("tranches", ())
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tranches": [
                {"month": str(t.created), "remaining": round(t.remaining, 6)}
                for t in self._tranches
            ]
        }

    @property
    def balance(self) -> float:
        return sum(t.remaining for t in self._tranches)

    def oldest_tranche_age(self, as_of: BillingMonth) -> int | None:
        """Months since the oldest surviving tranche was earned, or None if empty.

        The headroom indicator: at 12 it is about to expire. Reaching 11 means
        the account is one month away from starting to forfeit.
        """
        if not self._tranches:
            return None
        return as_of.months_since(self._tranches[0].created)

    def copy(self) -> DepositLedger:
        """Detached copy — projections must not mutate the real ledger."""
        return DepositLedger((t.created, t.remaining) for t in self._tranches)

    def settle(
        self, month: BillingMonth, earned: float, energy_cost: float
    ) -> MonthSettlement:
        """Run one month: expire, then consume FIFO, then credit what was earned."""
        forfeited, refunded = self._expire(month)
        used = self._consume(energy_cost)
        self._tranches.append(_Tranche(month, earned))
        return MonthSettlement(
            month=month,
            earned=earned,
            energy_cost=energy_cost,
            used=used,
            cash=energy_cost - used,
            refunded=refunded,
            forfeited=forfeited,
            balance=self.balance,
        )

    def _expire(self, month: BillingMonth) -> tuple[float, float]:
        """Drop tranches that reached 12 months. Returns (forfeited, refunded)."""
        forfeited = refunded = 0.0
        surviving = []
        for tranche in self._tranches:
            if month.months_since(tranche.created) >= self.VALIDITY_MONTHS:
                share = self.refund_share(tranche.created)
                refunded += share * tranche.remaining
                forfeited += (1 - share) * tranche.remaining
            else:
                surviving.append(tranche)
        self._tranches = surviving
        return forfeited, refunded

    @classmethod
    def refund_share(cls, earned_in: BillingMonth) -> float:
        """Cash-back share of an expired tranche, by the regime it was earned in."""
        return (
            cls._REFUND_SHARE_HOURLY
            if earned_in >= cls._HOURLY_SETTLEMENT_FROM
            else cls._REFUND_SHARE_MONTHLY
        )

    def _consume(self, energy_cost: float) -> float:
        """Spend oldest-first against the energy bill. Returns the amount used."""
        remaining_cost = energy_cost
        used = 0.0
        for tranche in self._tranches:
            if remaining_cost <= self._EPSILON:
                break
            taken = min(tranche.remaining, remaining_cost)
            tranche.remaining -= taken
            remaining_cost -= taken
            used += taken
        self._tranches = [t for t in self._tranches if t.remaining > self._EPSILON]
        return used


@dataclass(frozen=True)
class MonthSettlement:
    """Outcome of one settled month."""

    month: BillingMonth
    earned: float
    energy_cost: float
    used: float
    cash: float
    refunded: float
    forfeited: float
    balance: float


@dataclass
class _Tranche:
    """One month's deposit and what is left of it. Internal to the aggregate."""

    created: BillingMonth
    remaining: float
