"""What an imported kWh costs, per G13 zone — the URE decision as data.

Two cost components are tracked separately because they behave differently in
net-billing: the deposit offsets **active energy only**, while distribution is
always paid in cash. That split is also what sets the break-even RCE for the
"feed at peak, re-buy at night" strategy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

VAT: Final = 1.23


class Zone(StrEnum):
    """G13 tariff zones."""

    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


@dataclass(frozen=True)
class ZoneRates:
    """Net PLN/kWh for one month: active energy + variable distribution."""

    energy: Mapping[Zone, float]
    distribution: Mapping[Zone, float]

    def energy_cost(self, kwh: Mapping[Zone, float]) -> float:
        """Gross PLN for active energy — the part the deposit can offset."""
        return sum(kwh.get(z, 0.0) * self.energy[z] for z in Zone) * VAT

    def distribution_cost(self, kwh: Mapping[Zone, float]) -> float:
        """Gross PLN for variable distribution — always cash, never the deposit."""
        return sum(kwh.get(z, 0.0) * self.distribution[z] for z in Zone) * VAT

    def marginal_cost(self, zone: Zone) -> float:
        """Gross PLN/MWh of drawing 1 kWh from the grid in `zone`.

        The unit the evening planner reasons in: RCE is quoted per MWh and the
        user thinks in gross. Directly comparable with a gross RCE price,
        because the 1.23 factor applies to both sides of that comparison and
        cancels out.
        """
        return (self.energy[zone] + self.distribution[zone]) * VAT * 1000

    @property
    def night_marginal_cost(self) -> float:
        """Net PLN/kWh of a kWh re-bought at night (T3) — energy + distribution.

        This is the break-even RCE for exporting a stored kWh instead of keeping
        it: `gain = VAT * (RCE - night_marginal_cost)`, so the VAT factor cancels
        and the threshold is a plain price comparison.
        """
        return self.energy[Zone.T3] + self.distribution[Zone.T3]
