"""Day off: store the PV in the battery, or export it at once.

On a day off the whole day is T3 (`zone_for`), so the part of the evening
discharge that only covers the house is worth T3 (~0.63 zl gross), not T2.
When RCE is high all day, the PV stored at noon is worth less than the same
energy exported at once: 31.10.2026 (Saturday, RCE 1.11-1.26 zl gross in the
day, 1.39 at 16:00) the battery took 5.21 kWh and gave back 4.69 at 16:00, of
which 2.73 kWh only covered the house — -1.09 zl against an idle battery
(user's decision 01.11.2026, worked out in session score-framework-impl-4).

The rule simulates the day hour by hour for each variant and keeps the best:

- `NONE` — no charging from PV, the surplus goes to the grid at once;
- `S0` — as the charge start in force has it (from S0);
- `FROM_<hh>` — from a later whole hour, skipping the dearer ones before it.

Per hour: the battery takes the surplus (Live Soft minus p60 consumption of
earlier days of the same `DayType`) up to `MAX_KWH_PER_H` and up to full,
from the variant's start until T2. In the evening it gives back what it
holds above the floor (charged energy x `ROUND_TRIP`) in the discharge hours
of the day-off evening plan, up to `MAX_KWH_PER_H` each. The hour's balance
`PV - consumption - charge + discharge` is worth RCE (gross) when positive —
the deposit — and the zone's purchase price when negative: active energy
from the deposit, distribution in cash. A variant replaces `S0` only when
`MIN_ADVANTAGE_ZL` better, so noise in the forecast does not flip the start.

Pure: the service hands in hourly figures, the offline replay calls the same
`decide`. Kept free of `battery_schedule` (`live_reload` order).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Final

from .charge_curve import kwh_to_full
from .target_soc import MIN_SOC_PERCENT

if TYPE_CHECKING:
    from collections.abc import Sequence

# 31.10.2026: 53% -> 10% in 55 min, and the charge pace from PV alike.
MAX_KWH_PER_H: Final[float] = 5.0
# Round trip of a kWh through the battery (`target_soc.LOSS_FACTOR`).
ROUND_TRIP: Final[float] = 0.9
# A variant has to beat the one in force by this much (zl, deposit + cash).
# User's decision 01.11.2026: 0.05 — at 0.30 the rule would have let 31.10 charge
# (forecast -0.26 zl, actually -1.65), since the p60 consumption of Saturdays
# overstates the day almost twice and so understates the surplus.
MIN_ADVANTAGE_ZL: Final[float] = 0.05
# An hour counts as a later start only with a real surplus.
MIN_SURPLUS_KWH: Final[float] = 0.05

NONE: Final = "none"
S0: Final = "s0"


@dataclass(frozen=True)
class DayOffHour:
    """One hour of the day (kWh, zl/kWh gross)."""

    pv_kwh: float
    cons_kwh: float
    surplus_kwh: float
    rce_gross: float
    buy_energy: float
    buy_distribution: float


@dataclass(frozen=True)
class DayOffChargeDecision:
    """The variants of one day off, valued, and the one to apply."""

    day: date
    variants: tuple[VariantValue, ...]
    chosen: str
    in_force: str | None

    @classmethod
    def decide(  # noqa: PLR0913 — the day's inputs, one call site + the replay
        cls,
        day: date,
        hours: Sequence[DayOffHour],
        *,
        soc: float,
        s0_hour: int,
        t2_hour: int,
        discharge_hours: Sequence[int],
        in_force: str | None = None,
    ) -> DayOffChargeDecision:
        """Value every variant; keep `in_force` (or `S0`) unless beaten by the bar."""
        variants = [
            cls._value(NONE, None, hours, soc, t2_hour, discharge_hours),
            cls._value(S0, s0_hour, hours, soc, t2_hour, discharge_hours),
            *(
                cls._value(f"from_{h:02d}", h, hours, soc, t2_hour, discharge_hours)
                for h in range(s0_hour + 1, t2_hour)
                if hours[h].surplus_kwh > MIN_SURPLUS_KWH
            ),
        ]
        by_key = {v.key: v for v in variants}
        held = by_key.get(in_force or S0, by_key[S0])
        best = max(variants, key=lambda v: v.value_zl)
        chosen = (
            best.key if best.value_zl - held.value_zl >= MIN_ADVANTAGE_ZL else held.key
        )
        return cls(day=day, variants=tuple(variants), chosen=chosen, in_force=in_force)

    @staticmethod
    def _value(  # noqa: PLR0913, PLR0917 — one simulated day
        key: str,
        start_hour: int | None,
        hours: Sequence[DayOffHour],
        soc: float,
        t2_hour: int,
        discharge_hours: Sequence[int],
    ) -> VariantValue:
        room = kwh_to_full(soc)
        charge = [0.0] * len(hours)
        if start_hour is not None:
            for h in range(start_hour, t2_hour):
                taken = min(hours[h].surplus_kwh, MAX_KWH_PER_H, room)
                charge[h] = taken
                room -= taken
        charged = sum(charge)
        # What the battery holds above the floor at the evening: the morning's
        # level as it is, the day's charge less the round trip.
        pool = kwh_to_full(MIN_SOC_PERCENT) - kwh_to_full(soc) + charged * ROUND_TRIP
        out = [0.0] * len(hours)
        for h in discharge_hours:
            given = min(MAX_KWH_PER_H, pool)
            out[h] = given
            pool -= given
        deposit = cash = exported = 0.0
        for h, hour in enumerate(hours):
            net = hour.pv_kwh - hour.cons_kwh - charge[h] + out[h]
            if net > 0:
                deposit += net * hour.rce_gross
                exported += net
            else:
                deposit += net * hour.buy_energy
                cash += net * hour.buy_distribution
        return VariantValue(
            key=key,
            start_hour=start_hour,
            value_zl=round(deposit + cash, 2),
            deposit_zl=round(deposit, 2),
            cash_zl=round(cash, 2),
            charged_kwh=round(charged, 2),
            exported_kwh=round(exported, 2),
        )

    @property
    def changes_the_start(self) -> bool:
        """Tell whether the decision moves away from the start in force (S0)."""
        return self.chosen != S0

    def variant(self, key: str) -> VariantValue:
        return next(v for v in self.variants if v.key == key)

    def start_minute(self, t2_hour: int) -> int | None:
        """Charge start this decision puts in force: None = S0, T2 = no charging."""
        chosen = self.variant(self.chosen)
        if chosen.key == S0:
            return None
        if chosen.start_hour is None:
            return t2_hour * 60
        return chosen.start_hour * 60

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "chosen": self.chosen,
            "in_force": self.in_force,
            "variants": [v.to_dict() for v in self.variants],
        }


@dataclass(frozen=True)
class VariantValue:
    """One variant's day: value against the grid, split deposit / cash."""

    key: str
    start_hour: int | None
    value_zl: float
    deposit_zl: float
    cash_zl: float
    charged_kwh: float
    exported_kwh: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "start_hour": self.start_hour,
            "value_zl": self.value_zl,
            "deposit_zl": self.deposit_zl,
            "cash_zl": self.cash_zl,
            "charged_kwh": self.charged_kwh,
            "exported_kwh": self.exported_kwh,
        }
