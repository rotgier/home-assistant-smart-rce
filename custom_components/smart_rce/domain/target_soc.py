"""Target SOC formula + per-variant `TargetSoc` entity + VOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime  # noqa: TC003 — used in TargetSocContext at runtime
from typing import TYPE_CHECKING, Final

from .bucket import Bucket, Buckets

if TYPE_CHECKING:
    from .consumption_profiles import ConsumptionProfile
    from .pv_forecast import LivePvSignals, PvForecast


# --- Constants --- #

CONSUMPTION_PER_30MIN: Final[float] = 0.45  # kWh (= 0.9 kWh/h / 2)
BATTERY_CAPACITY_KWH: Final[float] = 10.7
MIN_SOC_PERCENT: Final[int] = 10
LOSS_FACTOR: Final[float] = 0.10  # 10% conversion losses
BUFFER_PERCENT: Final[int] = 12


# --- Per-variant TargetSoc entity --- #


@dataclass
class TargetSoc:
    """Per-variant target SOC entity — analog to `PvForecast` (variant concept).

    Holds `flat: TargetSocResult` (main, computed with default
    consumption profile) + `prev_days: list[TargetSocResult | None]`
    (one per prev-workday consumption profile, sensor attributes).
    Owns `recalculate()`, `max` aggregation, and `is_today` forwarded
    from its bound `PvForecast` variant.

    Plus persists `pv_profile` + `cons_view_flat` + `cons_views_prev` after
    each `recalculate()` — **in-memory only** (no sensor attrs), used by
    the matrix service for reads instead of recomputing per cell.

    Caller (TargetSocCatalog) passes `flat_cons` + `prev_cons` explicitly
    to `recalculate()` — TargetSoc doesn't know about
    `ConsumptionProfile.flat()` convention or the prev-days count.
    """

    variant: PvForecast
    flat: TargetSocResult | None = None
    prev_days: list[TargetSocResult | None] = field(default_factory=list)
    # In-memory cache for matrix reads (NOT exposed in sensor attrs).
    pv_profile: PvProfile | None = None
    cons_view_flat: ConsumptionProfile | None = None
    cons_views_prev: list[ConsumptionProfile | None] = field(default_factory=list)

    @property
    def is_today(self) -> bool:
        """Forwarded from PvForecast.is_today for convenience."""
        return self.variant.is_today

    @property
    def max(self) -> int | None:
        """Max target_soc across flat + prev_days (None when all None)."""
        vals = [r.value for r in [self.flat, *self.prev_days] if r is not None]
        return max(vals) if vals else None

    def recalculate(
        self,
        flat_cons: ConsumptionProfile,
        prev_cons: list[ConsumptionProfile | None],
        ctx: TargetSocContext,
    ) -> None:
        """Recompute flat + prev_days + persisted profiles for this variant.

        Builds `pv_profile` once (shared across all cons strategies), then
        `cons_view_flat` + `cons_views_prev` (per cons strategy, time-shifted
        when today + now_in_window). Computes flat + prev_days results from
        those profiles. Matrix service then reads these without recomputing.
        """
        result = self.variant.result
        if result is None:
            self.flat = None
            self.prev_days = [None] * len(prev_cons)
            self.pv_profile = None
            self.cons_view_flat = None
            self.cons_views_prev = [None] * len(prev_cons)
            return

        # Time-shift only when today AND now is inside the PV window (7-13).
        # Post-13 → fall back to full-window so the matrix renders sensible
        # cells (otherwise all today buckets would be past = sum 0).
        apply_now = self.variant.is_today and ctx.now_in_window
        if apply_now:
            if ctx.live_consumption_w is None or ctx.signals.pv_power_w is None:
                # Fail-hard: today inside window needs both live signals.
                self.flat = None
                self.prev_days = [None] * len(prev_cons)
                self.pv_profile = None
                self.cons_view_flat = None
                self.cons_views_prev = [None] * len(prev_cons)
                return
            self.pv_profile = result.to_profile(
                ctx.target_date,
                now=ctx.now,
                pv_power_w_5min=ctx.signals.pv_power_w,
            )
        else:
            self.pv_profile = result.to_profile(ctx.target_date)

        self.cons_view_flat = self._cons_view(flat_cons, ctx, apply_now)
        self.cons_views_prev = [
            self._cons_view(p, ctx, apply_now) if p is not None else None
            for p in prev_cons
        ]
        self.flat = self._compute(self.cons_view_flat, ctx)
        self.prev_days = [
            self._compute(cv, ctx) if cv is not None else None
            for cv in self.cons_views_prev
        ]

    def _cons_view(
        self, cons: ConsumptionProfile, ctx: TargetSocContext, apply_now: bool
    ) -> ConsumptionProfile:
        """Apply now-aware time-shift to consumption profile when in window."""
        if apply_now:
            return cons.to_view(now=ctx.now, live_consumption_w=ctx.live_consumption_w)
        return cons

    def _compute(
        self, cons_view: ConsumptionProfile, ctx: TargetSocContext
    ) -> TargetSocResult:
        """Single target_soc computation reusing cached pv_profile."""
        assert self.pv_profile is not None
        return _calculate_target_soc(
            self.pv_profile, cons_view, start_charge_hour=ctx.start_charge_hour
        )


# --- Input VOs --- #


@dataclass(frozen=True)
class TargetSocContext:
    """Per-call inputs for target_soc derivation.

    `TargetSocCatalog` builds two contexts (today + tomorrow) per recalc
    and dispatches the right one to each `TargetSoc` based on
    `entity.is_today`.

    `now_in_window` is True when `now` is within the PV window (7-13) AND
    the variant is for today. Triggers time-shift application (in-progress
    bucket prorated, past buckets skipped). Post-13 → False → full-window
    fallback (matrix doesn't go degenerate).
    """

    target_date: date
    signals: LivePvSignals
    live_consumption_w: float | None
    start_charge_hour: int | None
    now: datetime
    now_in_window: bool


@dataclass(frozen=True)
class TargetSocInputs:
    """Cons-side live + pre-charge gates — passed atomically to TargetSocCatalog.

    Pre-charge gates apply to `calculate_target_soc` so a sunny pre-charge
    hour's surplus cannot mask a later deficit (battery doesn't charge
    from PV when `battery_charge_max_current_toggle=False` — hourly
    surplus is exported, not stored).
    """

    live_consumption_w: float | None = None
    start_charge_hour_today: int | None = None
    start_charge_hour_tomorrow: int | None = None


# --- Output VOs --- #


@dataclass(frozen=True)
class TargetSocResult:
    """Target SOC + per-bucket trace + most-negative cumulative balance.

    `dip_kwh` is the absolute value of the lowest cumulative_balance reached
    during the 7:00..12:30 walk (0.0 when PV always covered consumption).
    Exposed as sensor attribute so recorder keeps history — Historical
    matrix can read it at any past `T` without recomputing from `buckets`.
    """

    value: int  # target SOC percent (MIN_SOC_PERCENT or higher)
    buckets: list[TargetSocBucket]
    dip_kwh: float


@dataclass(frozen=True)
class TargetSocBucket:
    """Per 30-min bucket trace entry used to verify target SOC calculation."""

    period: str  # "HH:MM" local
    pv_kwh: float
    cons_kwh: float
    balance: float
    cumulative: float
    is_min: bool  # True for bucket where cumulative is most negative


# --- PV profile (input to calculate_target_soc) --- #


@dataclass(frozen=True)
class PvProfile:
    """PV generation per 30-min bucket. Wraps a `Buckets` with PV-side role.

    Symmetric to `ConsumptionProfile`: strict 12-bucket contract over
    7:00..12:30 (validated inside `Buckets`), `.get(h, m)` returns float
    (no Optional). Build from a `PvForecastResult` via
    `PvForecastResult.to_profile(target_date)`.
    """

    buckets: Buckets

    def get(self, hour: int, minute: int) -> float:
        return self.buckets.get(hour, minute)

    @classmethod
    def flat(cls, value: float = 0.0) -> PvProfile:
        """Synthetic flat profile — every bucket = `value` kWh (default 0)."""
        return cls(buckets=Buckets.flat(value))

    @classmethod
    def from_realized_buckets(cls, realized: dict[tuple[int, int], float]) -> PvProfile:
        """Build PvProfile from RealizedPvLoader output (30-min bucket totals).

        Missing slots in the 7:00..12:30 window default to 0.0. Use case:
        prev-workday realized PV — no `PvForecastResult` is available, so
        `to_profile()` can't be called; this factory + `with_now_override`
        gives the same shape for symmetric apples-to-apples comparison
        with today's `PvForecastResult.to_profile(now, pv_w)` output.
        """
        by_bucket = {
            Bucket(h, m): realized.get((h, m), 0.0)
            for h in range(7, 13)
            for m in (0, 30)
        }
        return cls(buckets=Buckets(by_bucket=by_bucket))

    def with_now_override(
        self,
        now: datetime | None = None,
        pv_power_w_5min: float | None = None,
    ) -> PvProfile:
        """Return a now-aware view of this profile.

        Symmetric to `ConsumptionProfile.to_view`. Per-bucket transformation:
        - Closed bucket (bucket_end <= now): 0.0
        - In-progress bucket: `pv_power_w_5min × remaining_sec / 3_600_000`
        - Future bucket: unchanged

        When `now` is None the profile is returned unchanged (back-compat
        for tomorrow / matrix non-today). Fail-hard contract: pv_power_w_5min
        required when `now` is given (raises ValueError if None).
        """
        if now is None:
            return self
        if pv_power_w_5min is None:
            raise ValueError(
                "PvProfile.with_now_override: pv_power_w_5min required when now is given"
            )
        new_buckets = self.buckets.from_now(
            now, Bucket.live_remaining_kwh(now, pv_power_w_5min)
        )
        return PvProfile(buckets=new_buckets)


# --- Pure function (private — TargetSoc._compute is the sole caller) --- #


def _calculate_target_soc(
    pv_profile: PvProfile,
    consumption_profile: ConsumptionProfile,
    start_charge_hour: int | None = None,
) -> TargetSocResult:
    """Calculate target battery SOC + per-bucket trace.

    Pure cumulative-deficit sum over the 7:00..12:30 window. Each bucket
    contributes `pv_profile.get(h,m) - consumption_profile.get(h,m)`;
    the maximum cumulative deficit drives the SOC% needed.

    Time-awareness lives on the input profiles, not here — callers wanting
    "from now onwards" semantics pass profiles built via
    `PvForecastResult.to_profile(target_date, now, pv_power_w_5min)` and
    `ConsumptionProfile.to_view(now, live_consumption_w)`. Those methods
    bake the in-progress bucket prorate / live override into the bucket
    values directly. For full-window (tomorrow, prev-day) callers use the
    plain forecast / historical profile.

    `start_charge_hour` (int | None): pre-charge gate. When set, surplus
    accumulated during pre-charge hours (hour < start_charge_hour) does
    not carry over to the next hour. Battery doesn't charge from PV in
    pre-charge (battery_charge_max_current_toggle=False) — hourly surplus
    is exported, not stored. At each hour boundary where the prior hour
    was pre-charge, cumulative_balance is clamped to <= 0 (deficit kept,
    surplus zeroed). See context/target_soc_algorithm.md option A.
    """
    buckets: list[TargetSocBucket] = []
    cumulative_balance = 0.0
    min_balance = 0.0
    min_idx = -1
    prev_hour: int | None = None

    for hour in range(7, 13):
        for minute in (0, 30):
            # Hour-boundary clamp: if prior hour was in pre-charge, its surplus
            # was exported (not stored in battery) — zero out positive cumulative.
            if (
                prev_hour is not None
                and hour != prev_hour
                and start_charge_hour is not None
                and prev_hour < start_charge_hour
            ):
                cumulative_balance = min(cumulative_balance, 0.0)

            pv_kwh = pv_profile.get(hour, minute)
            cons_kwh = consumption_profile.get(hour, minute)
            balance = pv_kwh - cons_kwh
            cumulative_balance += balance
            if cumulative_balance < min_balance:
                min_balance = cumulative_balance
                min_idx = len(buckets)
            buckets.append(
                TargetSocBucket(
                    period=f"{hour:02d}:{minute:02d}",
                    pv_kwh=round(pv_kwh, 3),
                    cons_kwh=round(cons_kwh, 3),
                    balance=round(balance, 3),
                    cumulative=round(cumulative_balance, 3),
                    is_min=False,  # set below
                )
            )
            prev_hour = hour

    if min_idx >= 0:
        m = buckets[min_idx]
        buckets[min_idx] = TargetSocBucket(
            period=m.period,
            pv_kwh=m.pv_kwh,
            cons_kwh=m.cons_kwh,
            balance=m.balance,
            cumulative=m.cumulative,
            is_min=True,
        )

    dip_kwh = round(abs(min_balance), 3) if min_balance < 0 else 0.0

    if min_balance >= 0:
        return TargetSocResult(value=MIN_SOC_PERCENT, buckets=buckets, dip_kwh=dip_kwh)

    deficit_kwh = abs(min_balance)
    deficit_percent = deficit_kwh / (BATTERY_CAPACITY_KWH / 100)
    target = MIN_SOC_PERCENT + deficit_percent * (1 + LOSS_FACTOR) + BUFFER_PERCENT

    return TargetSocResult(
        value=max(round(target), MIN_SOC_PERCENT),
        buckets=buckets,
        dip_kwh=dip_kwh,
    )
