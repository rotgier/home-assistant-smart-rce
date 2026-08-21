"""Which net-billing regime applies to a given month.

Two rules change on the same date and are easy to let drift apart, so they live
together here: the coefficient applied to exported energy, and the cash share
refunded when a tranche expires. Boundary per `ZASADY_ROZLICZENIA.md` (confirmed
with TAURON): hourly RCE with the statutory 1.23 coefficient from 02.2025.
"""

from __future__ import annotations

from typing import Final

from .billing_month import BillingMonth

HOURLY_RCE_FROM: Final = BillingMonth(2025, 2)

_COEFFICIENT_HOURLY: Final = 1.23
_COEFFICIENT_EARLIER: Final = 1.0
_REFUND_SHARE_HOURLY: Final = 0.30
_REFUND_SHARE_EARLIER: Final = 0.20


def deposit_coefficient(month: BillingMonth) -> float:
    """Multiplier on the value of exported energy."""
    return _COEFFICIENT_HOURLY if month >= HOURLY_RCE_FROM else _COEFFICIENT_EARLIER


def refund_share(month: BillingMonth) -> float:
    """Cash-back share of a tranche that reached twelve months."""
    return _REFUND_SHARE_HOURLY if month >= HOURLY_RCE_FROM else _REFUND_SHARE_EARLIER
