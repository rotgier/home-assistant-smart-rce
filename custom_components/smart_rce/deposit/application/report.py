"""DepositReport — one snapshot of everything the context has to say.

Single DTO shared by the sensors (which read individual scalars) and by the
WebSocket command (which serialises the whole thing for the dashboard). Keeping
one snapshot avoids the sensors and the table disagreeing mid-recalculation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain.billing_month import BillingMonth
from ..domain.capacity import ConsumptionCapacity
from ..domain.deposit_ledger import MonthSettlement
from ..domain.projection import ExpiryOutlook, WinterOutlook
from ..domain.tariff import VAT


@dataclass(frozen=True)
class DepositReport:
    """Everything derived from the settled history, as of `last_settled`."""

    last_settled: BillingMonth
    balance: float
    capacity: ConsumptionCapacity
    oldest_tranche_age: int | None
    break_even_rce_net: float
    """Net PLN/MWh above which exporting a stored kWh beats keeping it for the night.

    Net because that is what the tariff is quoted in. Everything the user reads —
    `input_number.rce_high_price_threshold_gross`, the RCE sensors — is gross, so
    `break_even_rce_gross` is the headline; this stays for comparing against raw
    PSE quotes, which are net.
    """
    history: tuple[MonthSettlement, ...]
    winter: WinterOutlook
    expiry: ExpiryOutlook

    @property
    def utilization(self) -> float:
        return self.capacity.utilization(self.balance)

    @property
    def break_even_rce_gross(self) -> float:
        """Gross PLN/MWh — directly comparable with `rce_high_price_threshold_gross`.

        Equals the gross retail cost of the kWh re-bought at night, because the
        1.23 factor applies to both sides: earn RCE x 1.23 on export, pay
        (energy + distribution) x 1.23 on import.
        """
        return self.break_even_rce_net * VAT

    @property
    def peak_utilization(self) -> float | None:
        """Utilization at the next yearly peak (end of August) — the decision number.

        The current balance is the wrong gauge: it dips every winter by design.
        Forfeiting is decided at the yearly maximum, so that is what the strategy
        rule watches.
        """
        return self.expiry.peaks[0].utilization if self.expiry.peaks else None

    @property
    def peak_balance(self) -> float | None:
        return self.expiry.peaks[0].balance if self.expiry.peaks else None

    @property
    def first_forfeit_year(self) -> int | None:
        return self.expiry.first_forfeit.year if self.expiry.first_forfeit else None

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the dashboard. Rounded — this is a presentation payload."""
        return {
            "last_settled": str(self.last_settled),
            "balance": round(self.balance, 2),
            "capacity": round(self.capacity.value, 2),
            "utilization_pct": round(self.utilization, 1),
            "peak_utilization_pct": round(self.peak_utilization, 1)
            if self.peak_utilization is not None
            else None,
            "peak_balance": round(self.peak_balance, 2)
            if self.peak_balance is not None
            else None,
            "oldest_tranche_age_months": self.oldest_tranche_age,
            "break_even_rce_gross_pln_mwh": round(self.break_even_rce_gross),
            "break_even_rce_net_pln_mwh": round(self.break_even_rce_net),
            "first_forfeit": str(self.expiry.first_forfeit)
            if self.expiry.first_forfeit
            else None,
            "history": [_settlement(s) for s in self.history],
            "winter": {
                "covered": self.winter.covered,
                "cash_total": round(self.winter.cash_total, 2),
                "trough_month": str(self.winter.trough.month),
                "trough_balance": round(self.winter.trough.settlement.balance, 2),
                "months": [
                    {
                        "month": str(month.month),
                        "exported_kwh": round(month.exported_kwh, 1),
                        "import_kwh": round(month.import_kwh, 1),
                        "distribution_cost": round(month.distribution_cost, 2),
                        "available": round(month.available, 2),
                        **_settlement(month.settlement),
                    }
                    for month in self.winter.months
                ],
            },
            "peaks": [
                {
                    "year": peak.year,
                    "balance": round(peak.balance, 2),
                    "utilization_pct": round(peak.utilization, 1),
                    "forfeited": round(peak.forfeited, 2),
                    "refunded": round(peak.refunded, 2),
                }
                for peak in self.expiry.peaks
            ],
        }


def _settlement(settlement: MonthSettlement) -> dict[str, Any]:
    return {
        "month": str(settlement.month),
        "earned": round(settlement.earned, 2),
        "energy_cost": round(settlement.energy_cost, 2),
        "used": round(settlement.used, 2),
        "cash": round(settlement.cash, 2),
        "refunded": round(settlement.refunded, 2),
        "forfeited": round(settlement.forfeited, 2),
        "balance": round(settlement.balance, 2),
    }
