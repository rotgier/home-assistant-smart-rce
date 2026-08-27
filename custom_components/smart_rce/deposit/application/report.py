"""DepositReport — one snapshot of everything the context has to say.

Single DTO shared by the sensors (which read individual scalars) and by the
WebSocket command (which serialises the whole thing for the dashboard). Keeping
one snapshot avoids the sensors and the table disagreeing mid-recalculation.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
from typing import TYPE_CHECKING, Any

from ..domain.billing_month import BillingMonth
from ..domain.capacity import ConsumptionCapacity
from ..domain.deposit_ledger import MonthSettlement
from ..domain.projection import ExpiryOutlook, WinterOutlook
from ..domain.savings import SavingsReport
from ..domain.tariff import VAT

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class DepositReport:
    """Everything derived from the settled history, as of `last_settled`."""

    last_settled: BillingMonth
    balance: float
    """Balance after the last CLOSED month — the figure that reconciles with the invoice."""
    balance_running: float
    """Balance plus what the month in progress has accrued — "how much do I have now"."""
    last_data_day: datetime.date | None
    """Last day measured. Stale value here means the daily refresh stopped running."""
    elapsed_days: int
    unsettled_days: int
    """Stored days that look unbalanced. Anything but zero means the store is wrong."""
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
    volumes: Mapping[BillingMonth, MonthlyVolumes]
    """Measured energy behind each settled month — what the ledger was derived from."""
    winter: WinterOutlook
    expiry: ExpiryOutlook
    savings: SavingsReport

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
            "balance_running": round(self.balance_running, 2),
            "last_data_day": self.last_data_day.isoformat()
            if self.last_data_day
            else None,
            "elapsed_days": self.elapsed_days,
            "unsettled_days": self.unsettled_days,
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
            "history": [
                {**_settlement(s), **_volumes(self.volumes.get(s.month))}
                for s in self.history
            ],
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
            "savings": {
                "total": round(self.savings.total_pln, 2),
                "self_consumption": round(self.savings.self_consumption_pln, 2),
                "deposit": round(self.savings.deposit_pln, 2),
                "self_consumption_g11": round(self.savings.self_consumption_g11_pln, 2),
                "without_pv": round(self.savings.without_pv_pln, 2),
                "paid": round(self.savings.paid_pln, 2),
                "avoided": round(self.savings.avoided_pln, 2),
                "by_year": {
                    str(year): round(value, 2)
                    for year, value in self.savings.by_year().items()
                },
                "months": [
                    {
                        "month": str(month.month),
                        "self_consumption_kwh": round(month.self_consumption_kwh, 1),
                        "self_consumption": round(month.self_consumption_pln, 2),
                        "self_consumption_g11": None
                        if month.self_consumption_g11_pln is None
                        else round(month.self_consumption_g11_pln, 2),
                        "deposit": round(month.deposit_used_pln, 2),
                        "total": round(month.total_pln, 2),
                        "consumption_kwh": round(month.consumption_kwh, 1),
                        "without_pv": None
                        if month.without_pv_pln is None
                        else round(month.without_pv_pln, 2),
                        "paid": round(month.paid_variable_pln, 2),
                        "avoided": None
                        if month.avoided_pln is None
                        else round(month.avoided_pln, 2),
                    }
                    for month in self.savings.months
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


@dataclass(frozen=True)
class MonthlyVolumes:
    """The energy behind one settled month, and how the other regime would price it.

    Kept apart from `MonthSettlement`: that one is the ledger's verdict, this is
    what was measured. Joining them happens only at the serialisation boundary,
    where the dashboard wants one row per month.
    """

    exported_kwh: float
    import_kwh: float
    production_kwh: float | None
    """PV generated — measured from the recorder, seeded before it existed."""
    deposit_at_monthly_price: float | None
    """What the export would have earned under RCEm. None when no price is published."""


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


def _volumes(volumes: MonthlyVolumes | None) -> dict[str, Any]:
    if volumes is None:
        return {}
    return {
        "exported_kwh": round(volumes.exported_kwh, 1),
        "import_kwh": round(volumes.import_kwh, 1),
        "production_kwh": None
        if volumes.production_kwh is None
        else round(volumes.production_kwh, 1),
        "earned_at_monthly_price": None
        if volumes.deposit_at_monthly_price is None
        else round(volumes.deposit_at_monthly_price, 2),
    }
