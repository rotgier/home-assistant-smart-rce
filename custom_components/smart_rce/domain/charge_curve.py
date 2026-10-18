"""Charge curve — energy that goes into the battery per 1 pp of SoC, by SoC band.

Measured 2–12.10.2026 on the battery charge counter against SoC (934 pp,
112 kWh; `research/2026-10-11-backtest-poranne-ladowanie/`); 10–20% and the
top re-measured 18.10.2026 on PV and grid charge runs 09–18.10, the battery
power integral and the charge counter agreeing (home-assistant-ops
`research/2026-10-18-tempo-ladowania-pv/`):
- 10–20% takes 0.11–0.12 kWh/pp, not the 0.148 of the first fit;
- the top is no single band: above ~90% the BMS caps the power at ~2.1 kW
  and recalibrates the SoC — 89–91% takes ~0.2 kWh/pp, 91–98% only 0.03–0.06
  (the SoC jumps), 98–100% ~0.08.
With these the charge model's full charge on 6 sunny days: mean error +13 →
+3 min, MAE 19 → 7 min. 10→100% is ~9.9 kWh (the counter: ~10 kWh).

Two readers:
- the charge-start shift asks how much energy the battery still takes from the
  current SoC up to full (`kwh_to_full`),
- the PV surplus sensor asks how many pp below full a given surplus refills
  (`pp_below_full`) — "room for sun" without knowing today's SoC.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class SocBand:
    """SoC range `[soc_from, soc_to)` charged at `kwh_per_pp`."""

    soc_from: float
    soc_to: float
    kwh_per_pp: float

    def kwh_between(self, low: float, high: float) -> float:
        """Energy this band contributes to charging from `low` to `high`."""
        lo = max(low, self.soc_from)
        hi = min(high, self.soc_to)
        return (hi - lo) * self.kwh_per_pp if hi > lo else 0.0


BANDS: Final[tuple[SocBand, ...]] = (
    SocBand(10, 20, 0.115),
    SocBand(20, 30, 0.112),
    SocBand(30, 50, 0.114),
    SocBand(50, 75, 0.118),
    SocBand(75, 89, 0.110),
    SocBand(89, 91, 0.22),
    SocBand(91, 98, 0.04),
    SocBand(98, 100, 0.08),
)

FULL_SOC: Final[float] = 100.0


def kwh_per_pp(soc: float) -> float:
    """Energy one pp takes at `soc`; the edge bands hold outside 10-100%."""
    for band in BANDS:
        if band.soc_from <= soc < band.soc_to:
            return band.kwh_per_pp
    return BANDS[0].kwh_per_pp if soc < BANDS[0].soc_from else BANDS[-1].kwh_per_pp


def kwh_to_full(soc: float) -> float:
    """Energy the battery still takes from `soc` up to 100%."""
    return sum(band.kwh_between(soc, FULL_SOC) for band in BANDS)


def pp_below_full(kwh: float) -> float:
    """How many pp below 100% `kwh` refills — the bands walked down from the top."""
    left = kwh
    pp = 0.0
    for band in reversed(BANDS):
        if left <= 0:
            break
        band_kwh = (band.soc_to - band.soc_from) * band.kwh_per_pp
        if left >= band_kwh:
            pp += band.soc_to - band.soc_from
            left -= band_kwh
        else:
            pp += left / band.kwh_per_pp
            left = 0.0
    return pp
