"""What production actually did on a day — facts, kept apart from the counterfactual ranking.

The ranking asks "what would the rule have chosen with this forecast"; this
panel says what happened: when charging really started, when (if) the battery
got full, how much the afternoon grid charge had to top up, what went out in
the morning and what came in during T1.

Grid flows are net-billing hourly balances (sum of the hour's 5-minute net,
then the sign): a 5-minute import inside an hour that exports overall is no
import.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time, timedelta
from typing import TYPE_CHECKING, Any, Final

from ..charge_curve import kwh_to_full
from .day_actuals import CONSUMPTION, GRID_NET_EXPORT, PV

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .day_actuals import DayActuals

T1_HOURS: Final = range(7, 13)
MORNING_EXPORT_HOURS: Final = range(7, 10)
# A slot mean at or above this counts as full (the mean of the slot the
# battery reaches 100 in is just below it).
FULL_SOC: Final = 99.5
SOC_RISE_PP: Final = 1.0


def production_outcome(
    actuals: DayActuals,
    start: ChargeStartFact,
    afternoon_charge: Sequence[tuple[int, int]] | None,
) -> ProductionOutcome:
    """Facts of the day.

    `afternoon_charge`: CHARGE_AFTERNOON slot spans (minutes of the day), None
    when unknown (history gone) — the top-up is then unknown, not zero.
    """
    full = actuals.first_soc_at_least(FULL_SOC)
    t1_soc = actuals.soc_between(time(7, 0), time(13, 0))
    topups = (
        tuple(_topup(actuals, a, b) for a, b in afternoon_charge)
        if afternoon_charge is not None
        else None
    )
    return ProductionOutcome(
        start=start,
        full_at=full.hour * 60 + full.minute if full is not None else None,
        min_soc_t1=round(min(t1_soc), 1) if t1_soc else None,
        t1_import_kwh=_hourly_sum(actuals, T1_HOURS, import_=True),
        morning_export_kwh=_hourly_sum(actuals, MORNING_EXPORT_HOURS, import_=False),
        topups=topups,
    )


def struck_start(changes: Sequence[tuple[int, int]]) -> int | None:
    """When the charge start really struck, from the day's start-setting history.

    `changes`: (minute set, start minute) in time order — the state of the
    start entity over the day. A start strikes when the clock reaches it while
    it is in force; one set already in the past strikes at once.
    """
    for i, (since, start) in enumerate(changes):
        until = changes[i + 1][0] if i + 1 < len(changes) else 24 * 60
        struck = max(start, since)
        if struck < until:
            return struck
    return None


def start_from_soc_rise(actuals: DayActuals) -> int | None:
    """Fallback start: the first 07:00+ slot from which SoC climbs 1 pp in 10 min.

    Before the start the battery exports the surplus, so the first sustained
    rise after the morning grid charge marks the start — to the 5 minutes.
    """
    slots = [
        (s, v)
        for s, v in actuals.soc_series()
        if v is not None and time(7, 0) <= s.time() < time(16, 0)
    ]
    for (start, a), (_s1, b), (_s2, c) in zip(
        slots, slots[1:], slots[2:], strict=False
    ):
        if b > a and c - a >= SOC_RISE_PP:
            return start.hour * 60 + start.minute
    return None


def _hourly_sum(actuals: DayActuals, hours: range, *, import_: bool) -> float | None:
    """Sum imports (or exports) of whole-hour net balances over `hours`."""
    balances = [actuals.hourly(GRID_NET_EXPORT, h) for h in hours]
    known = [b for b in balances if b is not None]
    if not known:
        return None
    sign = -1.0 if import_ else 1.0
    return round(sum(max(sign * b, 0.0) for b in known), 3)


def _topup(actuals: DayActuals, first: int, end: int) -> GridTopup:
    """Energy the afternoon charge put in from the grid, beyond the PV surplus.

    battery = energy from the SoC before the slot to the SoC at its end
    (charge curve); PV surplus = max(0, PV − consumption without water
    heating) over the slot; the top-up is what the surplus cannot explain.
    """
    begin_at, end_at = actuals.at(first), actuals.at(end)
    before = actuals.soc_near(begin_at - timedelta(minutes=1))
    after = actuals.soc_near(end_at - timedelta(minutes=1))
    battery = (
        max(0.0, kwh_to_full(before) - kwh_to_full(after))
        if before is not None and after is not None
        else None
    )
    surplus = max(
        0.0,
        sum(actuals.series_between(PV, begin_at, end_at))
        - sum(actuals.series_between(CONSUMPTION, begin_at, end_at)),
    )
    hours = range(first // 60, (end - 1) // 60 + 1)
    return GridTopup(
        first=first,
        end=end,
        soc_before=before,
        soc_after=after,
        battery_kwh=round(battery, 3) if battery is not None else None,
        pv_surplus_kwh=round(surplus, 3),
        hourly_import_kwh=_hourly_sum(actuals, hours, import_=True),
    )


# --- Value objects --- #


@dataclass(frozen=True)
class ChargeStartFact:
    """When the battery really started taking PV, and where that is known from.

    `source`: "shift" (the persisted forecast shift of the day), "override"
    (history of the start-hour override entity), "soc_rise" (first sustained
    SoC rise — days before either was kept), "none".
    """

    start: int | None  # minute of the day
    source: str
    s0: int | None = None
    locked: bool | None = None
    manual: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "source": self.source,
            "s0": self.s0,
            "locked": self.locked,
            "manual": self.manual,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ChargeStartFact:
        return cls(
            start=data.get("start"),
            source=str(data.get("source", "none")),
            s0=data.get("s0"),
            locked=data.get("locked"),
            manual=data.get("manual"),
        )


@dataclass(frozen=True)
class GridTopup:
    """One CHARGE_AFTERNOON span: what went into the battery and from where."""

    first: int  # minute of the day
    end: int
    soc_before: float | None
    soc_after: float | None
    battery_kwh: float | None
    pv_surplus_kwh: float
    hourly_import_kwh: float | None  # net imports of the hours the span touches

    @property
    def grid_kwh(self) -> float | None:
        """Battery energy the PV surplus cannot explain — taken from the grid."""
        if self.battery_kwh is None:
            return None
        return round(max(0.0, self.battery_kwh - self.pv_surplus_kwh), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "first": self.first,
            "end": self.end,
            "soc_before": self.soc_before,
            "soc_after": self.soc_after,
            "battery_kwh": self.battery_kwh,
            "pv_surplus_kwh": self.pv_surplus_kwh,
            "hourly_import_kwh": self.hourly_import_kwh,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GridTopup:
        return cls(
            first=int(data["first"]),
            end=int(data["end"]),
            soc_before=data.get("soc_before"),
            soc_after=data.get("soc_after"),
            battery_kwh=data.get("battery_kwh"),
            pv_surplus_kwh=float(data["pv_surplus_kwh"]),
            hourly_import_kwh=data.get("hourly_import_kwh"),
        )


@dataclass(frozen=True)
class ProductionOutcome:
    """Facts of the day (minutes of the day, %, kWh)."""

    start: ChargeStartFact
    full_at: int | None
    min_soc_t1: float | None
    t1_import_kwh: float | None
    morning_export_kwh: float | None
    topups: tuple[GridTopup, ...] | None  # None = afternoon charge spans unknown

    @property
    def grid_topup_kwh(self) -> float | None:
        """Grid energy of all afternoon charges; 0 when there was none."""
        if self.topups is None:
            return None
        if not self.topups:
            return 0.0
        values = [t.grid_kwh for t in self.topups if t.grid_kwh is not None]
        return round(sum(values), 3) if values else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.to_dict(),
            "full_at": self.full_at,
            "min_soc_t1": self.min_soc_t1,
            "t1_import_kwh": self.t1_import_kwh,
            "morning_export_kwh": self.morning_export_kwh,
            "topups": (
                [t.to_dict() for t in self.topups] if self.topups is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProductionOutcome:
        return cls(
            start=ChargeStartFact.from_dict(data.get("start", {})),
            full_at=data.get("full_at"),
            min_soc_t1=data.get("min_soc_t1"),
            t1_import_kwh=data.get("t1_import_kwh"),
            morning_export_kwh=data.get("morning_export_kwh"),
            topups=(
                tuple(GridTopup.from_dict(t) for t in raw)
                if (raw := data.get("topups")) is not None
                else None
            ),
        )
