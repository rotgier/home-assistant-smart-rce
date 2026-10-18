"""Accuracy so far — how the morning's forecasts are doing before the day closes.

The full scores need the day's data to its end (D1 to 13:00, D2 to T2); the
recomputations at the charge start and at S0 come earlier. What they can show
honestly is the part of the T1 window already measured: for every reserve
moment, the energy each forecast promised over [max(07:00, moment), until)
against what came, and the same for the consumption model the system read.

Signs follow the reserve (− = the system would keep too little): a PV forecast
above what came lowers the reserve, so its part is −(forecast − actual); a
consumption model above what came raises it, so its part is +(model − actual).
The points are kWh through `soc_points` — a linear stand-in for the reserve's
own dip arithmetic, good for "how far off so far", not for the final score.

The same table on a closed day covers the whole window (until 13:00).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final

from ..bucket import PV_WINDOW_HOURS
from ..consumption_profiles import ConsumptionProfile
from .day_actuals import CONSUMPTION, PV
from .reserve_score import RESERVE_MOMENTS, soc_points
from .variant_results import SCORED_VARIANTS, consumed

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ..pv_forecast import PvForecastResult
    from .day_actuals import DayActuals
    from .production_setup import ProductionSetup
    from .variant_results import ScoredSnapshot

# The T1 window the reserve covers.
WINDOW_START: Final = time(PV_WINDOW_HOURS[0])
WINDOW_END: Final = time(PV_WINDOW_HOURS[-1] + 1)
_BUCKET: Final = timedelta(minutes=30)


def score_so_far(
    actuals: DayActuals,
    snapshots: Sequence[ScoredSnapshot],
    earlier_workdays: Sequence[Mapping[int, float]],
    setup: ProductionSetup,
    until: datetime | None,
) -> tuple[MomentSoFar, ...]:
    """Score every reserve moment over the measured part of the window.

    `until`: how far the data reaches (None = the whole window). A moment with
    nothing measured after it yet is left out.
    """
    day, tz = actuals.day, actuals.tz
    window_end = datetime.combine(day, WINDOW_END, tz)
    end = window_end if until is None else min(until, window_end)
    model = setup.reserve_model.reserve_profile(earlier_workdays)
    profile = model or ConsumptionProfile.flat()
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
        expected_cons = _profile_kwh(profile, since, end)
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
                flat_model=model is None,
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
    flat_model: bool  # no earlier workdays: the flat 0.9 kWh/h stood in

    def variant(self, key: str) -> VariantSoFar | None:
        return next((v for v in self.variants if v.variant == key), None)

    def pv_error_kwh(self, key: str) -> float | None:
        """Forecast − actual PV of one variant (+ = promised more than came)."""
        found = self.variant(key)
        return round(found.pv_kwh - self.pv_actual_kwh, 3) if found else None

    def pv_part_pp(self, key: str) -> int | None:
        """Reserve effect of that PV error in SoC points (− = too little)."""
        error = self.pv_error_kwh(key)
        return -round(soc_points(error)) if error is not None else None

    @property
    def consumption_error_kwh(self) -> float:
        """Model − actual consumption (+ = expected more than came)."""
        return round(self.consumption_model_kwh - self.consumption_actual_kwh, 3)

    @property
    def consumption_part_pp(self) -> int:
        """Reserve effect of the consumption error (− = too little)."""
        return round(soc_points(self.consumption_error_kwh))

    @property
    def system_part_pp(self) -> int | None:
        """The system's PV and consumption parts together."""
        pv = self.pv_part_pp(self.system_variant)
        return pv + self.consumption_part_pp if pv is not None else None

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
