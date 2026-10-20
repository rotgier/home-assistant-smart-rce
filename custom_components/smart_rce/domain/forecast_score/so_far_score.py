"""Accuracy so far — how the morning's forecasts are doing before the day closes.

The full scores need the day's data to its end (D1 to 13:00, D2 to T2); the
recomputations at the charge start and at S0 come earlier. What they can show
honestly is the part of the T1 window already measured: for every reserve
moment, the energy each forecast promised over [max(07:00, moment), until)
against what came, and the same for the consumption model the system read.

Errors are in kWh and percent of what came, + = the forecast (or the
consumption model) promised MORE than came — the plain sense, not the reserve's
sign. Deliberately no SoC points: the reserve depends on the dip until the sun
covers the house, not on the window's whole energy (17.10 at 06:11 the window
said −40 "points" where the reserve's PV and consumption parts were −1 and 0).
The reserve's own points stay in D1.

The consumption model is the one production read that day: on a day with a T1
reserve the reserve's (p60 of earlier workdays); on a day off — no reserve —
the charge start's (p60 of earlier days of the same type: Saturdays, Sundays).
A rule that is no profile (the old max-of rule) or no history: the flat
0.9 kWh/h.

The same table on a closed day covers the whole window (until 13:00). A second
one runs on to 16:00 (`DAY_END`) — the rest of the charging day; its expected
consumption is the model's half hours (flat 0.45 kWh where it has none), since
the reserve's profile stops at 13:00.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from ..bucket import PV_WINDOW_HOURS
from ..consumption_profiles import ConsumptionProfile
from ..target_soc import CONSUMPTION_PER_30MIN
from .day_actuals import CONSUMPTION, PV
from .reserve_score import RESERVE_MOMENTS
from .variant_results import SCORED_VARIANTS, consumed

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from ..pv_forecast import PvForecastResult
    from .consumption_models import ConsumptionModel
    from .day_actuals import DayActuals
    from .production_setup import ProductionSetup
    from .variant_results import ScoredSnapshot

# The T1 window the reserve covers.
WINDOW_START: Final = time(PV_WINDOW_HOURS[0])
WINDOW_END: Final = time(PV_WINDOW_HOURS[-1] + 1)
# The longer window: the charging day.
DAY_END: Final = time(16)
_BUCKET: Final = timedelta(minutes=30)


def score_so_far(  # noqa: PLR0917 — one call site, the day's inputs
    actuals: DayActuals,
    snapshots: Sequence[ScoredSnapshot],
    consumption: ConsumptionModel,
    earlier_days: Sequence[Mapping[int, float]],
    model_days: str,
    setup: ProductionSetup,
    until: datetime | None,
    window_end_at: time = WINDOW_END,
) -> tuple[MomentSoFar, ...]:
    """Score every reserve moment over the measured part of the window.

    `consumption` learns from `earlier_days`, of the kind `model_days` names
    (workday, saturday, sunday). `until`: how far the data reaches (None = the
    whole window). The window ends at `window_end_at`. A moment with nothing
    measured after it yet is left out.
    """
    day, tz = actuals.day, actuals.tz
    window_end = datetime.combine(day, window_end_at, tz)
    end = window_end if until is None else min(until, window_end)
    flat: bool
    expected: Callable[[datetime, datetime], float]
    if window_end_at == WINDOW_END:
        reserve_profile = consumption.reserve_profile(earlier_days)
        flat = reserve_profile is None
        profile = reserve_profile or ConsumptionProfile.flat()
        expected = partial(_profile_kwh, profile)
    else:
        slots = consumption.half_hours(earlier_days)
        flat = slots is None
        expected = partial(_slots_kwh, slots or {})
    out: list[MomentSoFar] = []
    for moment in RESERVE_MOMENTS:
        at = datetime.combine(day, moment.at, tz)
        snapshot = consumed(snapshots, at)
        since = max(at, datetime.combine(day, WINDOW_START, tz))
        if snapshot is None or end <= since:
            continue
        pv = actuals.energy_between(PV, since, end)
        cons = actuals.energy_between(CONSUMPTION, since, end)
        if pv is None or cons is None:
            continue
        expected_cons = expected(since, end)
        out.append(
            MomentSoFar(
                label=moment.label,
                snapshot_at=snapshot.taken_at,
                since=since,
                until=end,
                pv_actual_kwh=round(pv, 3),
                consumption_actual_kwh=round(cons, 3),
                consumption_model_kwh=round(expected_cons, 3),
                variants=tuple(
                    VariantSoFar(variant=key, pv_kwh=round(kwh, 3))
                    for key in SCORED_VARIANTS
                    if (result := snapshot.result(key)) is not None
                    and (kwh := _forecast_kwh(result, since, end)) is not None
                ),
                system_variant=setup.pv_variant,
                flat_model=flat,
                model_days=model_days,
                model_day_count=0 if flat else len(earlier_days),
            )
        )
    return tuple(out)


def _profile_kwh(profile: ConsumptionProfile, since: datetime, end: datetime) -> float:
    """Return the expected consumption over [since, end), edge buckets in proportion."""
    day, tz = since.date(), since.tzinfo
    total = 0.0
    for bucket, kwh in profile.buckets.by_bucket.items():
        start = datetime.combine(day, time(bucket.hour, bucket.minute), tz)
        total += kwh * _share(start, since, end)
    return total


def _slots_kwh(slots: Mapping[int, float], since: datetime, end: datetime) -> float:
    """Return the expected consumption over [since, end) from half-hour slots."""
    day, tz = since.date(), since.tzinfo
    return sum(
        slots.get(slot, CONSUMPTION_PER_30MIN)
        * _share(datetime.combine(day, time(slot // 2, slot % 2 * 30), tz), since, end)
        for slot in range(48)
    )


def _forecast_kwh(
    result: PvForecastResult, since: datetime, end: datetime
) -> float | None:
    """Return the forecast energy over [since, end) — the archive holds rates (kWh/h)."""
    total = 0.0
    found = False
    for period in result.forecast:
        start = datetime.fromisoformat(str(period.period_start).replace(" ", "T"))
        share = _share(start, since, end)
        if share > 0:
            total += period.pv_estimate_adjusted / 2 * share
            found = True
    return total if found else None


def _share(start: datetime, since: datetime, end: datetime) -> float:
    """Return the part of the half hour from `start` that falls in [since, end)."""
    overlap = min(start + _BUCKET, end) - max(start, since)
    return max(0.0, overlap / _BUCKET)


# --- Value objects --- #


@dataclass(frozen=True)
class MomentSoFar:
    """One reserve moment over the measured part of the window (kWh)."""

    label: str
    snapshot_at: datetime
    since: datetime
    until: datetime
    pv_actual_kwh: float
    consumption_actual_kwh: float
    consumption_model_kwh: float
    variants: tuple[VariantSoFar, ...]
    system_variant: str  # the PV variant production steered by that day
    flat_model: bool  # no earlier days: the flat 0.9 kWh/h stood in
    model_days: str = "workday"  # the kind of earlier days the model learned from
    model_day_count: int = 0

    def variant(self, key: str) -> VariantSoFar | None:
        return next((v for v in self.variants if v.variant == key), None)

    def pv_error_kwh(self, key: str) -> float | None:
        """Forecast − actual PV of one variant (+ = promised more than came)."""
        found = self.variant(key)
        return round(found.pv_kwh - self.pv_actual_kwh, 3) if found else None

    def pv_error_pct(self, key: str) -> float | None:
        """Return the PV error in percent of what came (None when nothing came)."""
        error = self.pv_error_kwh(key)
        if error is None or self.pv_actual_kwh <= 0:
            return None
        return round(100 * error / self.pv_actual_kwh, 1)

    @property
    def consumption_error_kwh(self) -> float:
        """Model − actual consumption (+ = expected more than came)."""
        return round(self.consumption_model_kwh - self.consumption_actual_kwh, 3)

    @property
    def consumption_error_pct(self) -> float | None:
        """Return the consumption error in percent of what came (None: nothing)."""
        if self.consumption_actual_kwh <= 0:
            return None
        return round(100 * self.consumption_error_kwh / self.consumption_actual_kwh, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "snapshot_at": self.snapshot_at.isoformat(),
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "pv_actual_kwh": self.pv_actual_kwh,
            "consumption_actual_kwh": self.consumption_actual_kwh,
            "consumption_model_kwh": self.consumption_model_kwh,
            "variants": [v.to_dict() for v in self.variants],
            "system_variant": self.system_variant,
            "flat_model": self.flat_model,
            "model_days": self.model_days,
            "model_day_count": self.model_day_count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MomentSoFar:
        return cls(
            label=str(data["label"]),
            snapshot_at=datetime.fromisoformat(data["snapshot_at"]),
            since=datetime.fromisoformat(data["since"]),
            until=datetime.fromisoformat(data["until"]),
            pv_actual_kwh=float(data["pv_actual_kwh"]),
            consumption_actual_kwh=float(data["consumption_actual_kwh"]),
            consumption_model_kwh=float(data["consumption_model_kwh"]),
            variants=tuple(VariantSoFar.from_dict(v) for v in data["variants"]),
            system_variant=str(data["system_variant"]),
            flat_model=bool(data.get("flat_model", False)),
            model_days=str(data.get("model_days", "workday")),
            model_day_count=int(data.get("model_day_count", 0)),
        )


@dataclass(frozen=True)
class VariantSoFar:
    """What one PV forecast promised over the span (kWh)."""

    variant: str
    pv_kwh: float

    def to_dict(self) -> dict[str, Any]:
        return {"variant": self.variant, "pv_kwh": self.pv_kwh}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VariantSoFar:
        return cls(variant=str(data["variant"]), pv_kwh=float(data["pv_kwh"]))
