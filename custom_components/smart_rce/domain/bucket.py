"""Bucket vocabulary — `Bucket` VO + `Buckets` 12-bucket collection.

Two value objects model the 30-min PV-window grid (7:00..12:30):

- `Bucket(hour, minute)` — identity of a single 30-min slot. Owns time
  math (`is_closed_at`, `is_in_progress_at`, `is_future_at`,
  `remaining_sec_at`, `Bucket.enclosing(now)`) and in-progress kWh
  arithmetic as @staticmethods (`live_remaining_kwh`, `full_bucket_kwh`).

- `Buckets({Bucket: kWh})` — the full 12-bucket mapping. Strict
  contract: exactly 12 entries covering 7:00..12:30. Single point of
  validation for what was previously duplicated `_EXPECTED_BUCKETS`
  guards in `PvProfile` + `ConsumptionProfile`. Owns the structural
  time-shift transform `from_now()` (replaces module-level
  `buckets_from_now()`) and the `flat()` factory.

`PvProfile` and `ConsumptionProfile` compose a `Buckets` field — they
add semantic role (PV vs cons) and optional metadata (source_date for
cons) on top of a shared storage + validation primitive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Final


@dataclass(frozen=True)
class Bucket:
    """A 30-min interval on the 7-13 PV-window grid. Identity = (hour, minute).

    Holds only its position in time and the time-arithmetic that follows
    from it. Energy values (kWh, kWh/h rates) live in other types
    (`AdjustedPeriod`, `Buckets`) — `Bucket` does not own values.

    `minute` is restricted to {0, 30} — half-hour grid. Outside callers
    construct `Bucket(h, m)` directly; for the bucket enclosing a wall
    clock value use `Bucket.enclosing(now)`. kWh helpers
    (`live_remaining_kwh`, `full_bucket_kwh`) are `@staticmethod` — they
    operate on the bucket enclosing `now` implicitly, no instance needed.
    """

    hour: int
    minute: int  # 0 or 30

    def __post_init__(self) -> None:
        if self.minute not in (0, 30):
            raise ValueError(f"Bucket minute must be 0 or 30, got {self.minute}")

    @classmethod
    def enclosing(cls, now: datetime) -> Bucket:
        """Bucket that contains `now`. Always returns a valid 30-min slot."""
        return cls(now.hour, 0 if now.minute < 30 else 30)

    def is_closed_at(self, now: datetime) -> bool:
        """Return True when `now` is at or past the bucket's end."""
        return now >= self._end_datetime(now)

    def is_in_progress_at(self, now: datetime) -> bool:
        """Return True when bucket_start <= now < bucket_end."""
        start = self._start_datetime(now)
        return start <= now < start + timedelta(minutes=30)

    def is_future_at(self, now: datetime) -> bool:
        """Return True when bucket_start > now (entirely ahead of `now`)."""
        return now < self._start_datetime(now)

    def remaining_sec_at(self, now: datetime) -> float:
        """Seconds from `now` to bucket_end. Negative if `now` past bucket_end.

        For `now` before bucket_start returns the full 1800s (caller
        typically uses this on the enclosing bucket where it lies in
        [0, 1800]).
        """
        return (self._end_datetime(now) - now).total_seconds()

    @staticmethod
    def full_bucket_kwh(
        now: datetime,
        power_w: float,
        bucket_so_far_kwh: float,
        *,
        derivative_w_per_min: float = 0.0,
    ) -> float:
        """Total in-progress bucket kWh = realized so-far + extrapolated remaining.

        The "what we expect this bucket to deliver" estimate. Shared by
        chart display (rescaled to kWh/h rate via × 2 inside
        `AdjustedPvForecast.with_now_aware_in_progress*`) and strategy
        score input in extrapolation variants.

        With `derivative_w_per_min != 0`, the remaining-time integral uses
        a linear ramp `P(t) = power_w + r·t` instead of constant power.
        Used by derivative-aware projection (Phase C) when the PV first
        derivative signal is flagged stable. Default keeps constant
        behaviour for all current callers.
        """
        return bucket_so_far_kwh + Bucket.live_remaining_kwh(
            now, power_w, derivative_w_per_min=derivative_w_per_min
        )

    @staticmethod
    def live_remaining_kwh(
        now: datetime,
        power_w: float,
        *,
        derivative_w_per_min: float = 0.0,
    ) -> float:
        """Energy contribution from `now` to end of the enclosing 30-min bucket.

        `derivative_w_per_min == 0` (default): `power_w` holds constant
        over the remaining time. Returns `(power_w / 1000) · T / 3600`
        kWh.

        `derivative_w_per_min != 0`: linear ramp `P(t) = power_w + r·t`
        where `r = derivative_w_per_min / 60` W/sec. Integrated from 0
        to T: `E = P·T + r·T²/2`. Caller is responsible for gating on
        a stability signal — passing a non-zero derivative on a noisy
        signal will over-/under-estimate.

        Single source of truth for the in-progress bucket integration
        consumed by:
        - `AdjustedPvForecast.to_profile` (PV side, in-progress = remaining
          only; past contributes 0 to the forward-looking deficit).
        - `ConsumptionProfile.to_view` (consumption side, same shape).
        - `Bucket.full_bucket_kwh` (chart display, combined with so_far).
        - `pv_forecast_extrapolation._compute_*_score` indirectly via
          `Bucket.full_bucket_kwh` (realized rate = full_bucket × 2).
        - `PvForecastSensor` observability variants in Phase C (constant
          vs derivative-aware projection of the in-progress bucket).
        """
        remaining_sec = Bucket.enclosing(now).remaining_sec_at(now)
        if derivative_w_per_min == 0.0:
            return (power_w / 1000.0) * remaining_sec / 3600.0
        r_w_per_sec = derivative_w_per_min / 60.0
        energy_w_sec = power_w * remaining_sec + 0.5 * r_w_per_sec * remaining_sec**2
        return energy_w_sec / (3600.0 * 1000.0)

    # --- common helpers --- #

    def _end_datetime(self, on: datetime) -> datetime:
        """Bucket end as a tz-aware datetime on `on`'s date (= start + 30 min)."""
        return self._start_datetime(on) + timedelta(minutes=30)

    def _start_datetime(self, on: datetime) -> datetime:
        """Bucket start as a tz-aware datetime on `on`'s date + tzinfo."""
        return datetime.combine(
            on.date(), time(hour=self.hour, minute=self.minute), tzinfo=on.tzinfo
        )


# Canonical 12-bucket set for the 7:00..12:30 PV window. Used by
# `Buckets.__post_init__` validation and by `Buckets.flat` construction.
_PV_WINDOW_BUCKETS: Final[frozenset[Bucket]] = frozenset(
    Bucket(h, m) for h in range(7, 13) for m in (0, 30)
)


@dataclass(frozen=True)
class Buckets:
    """12-bucket {Bucket: kWh} mapping covering 7:00..12:30.

    Strict contract — exactly 12 entries, one per (hour, minute) slot in
    the PV window. Single source of validation that used to live as
    duplicated `_EXPECTED_BUCKETS` checks in `PvProfile` and
    `ConsumptionProfile.__post_init__`.

    Storage is `dict[Bucket, float]` (not `dict[tuple[int, int], float]`)
    so iteration yields rich `Bucket` instances directly — callers can
    invoke `bucket.is_in_progress_at(now)` etc. without re-wrapping. The
    `.get(hour, minute)` accessor lets numeric callers stay terse.
    """

    by_bucket: dict[Bucket, float]

    def __post_init__(self) -> None:
        got = frozenset(self.by_bucket.keys())
        if got != _PV_WINDOW_BUCKETS:
            missing = sorted((b.hour, b.minute) for b in _PV_WINDOW_BUCKETS - got)
            extra = sorted((b.hour, b.minute) for b in got - _PV_WINDOW_BUCKETS)
            raise ValueError(
                "Buckets must cover exactly 7:00..12:30 (12 slots); "
                f"missing={missing}, extra={extra}"
            )

    def __iter__(self):
        """Iterate `Bucket` keys (matches dict iteration semantics)."""
        return iter(self.by_bucket)

    @classmethod
    def flat(cls, value: float) -> Buckets:
        """Synthetic snapshot — every bucket = `value` kWh."""
        return cls(by_bucket={bucket: value for bucket in _PV_WINDOW_BUCKETS})

    def get(self, hour: int, minute: int) -> float:
        """Lookup by (hour, minute) tuple — convenience for numeric callers."""
        return self.by_bucket[Bucket(hour, minute)]

    def keys(self):
        """Iterate over `Bucket` keys (no value)."""
        return self.by_bucket.keys()

    def values(self):
        """Iterate over kWh values (no key)."""
        return self.by_bucket.values()

    def items(self):
        """Iterate over `(Bucket, kWh)` pairs."""
        return self.by_bucket.items()

    def from_now(self, now: datetime, live_remaining_kwh: float) -> Buckets:
        """Project the snapshot onto the "from-now" view.

        Per bucket (classification by `Bucket.is_*_at(now)`):
        - closed → 0.0
        - in-progress → `live_remaining_kwh`
        - future → unchanged

        `live_remaining_kwh` is the kWh contribution from `now` to
        bucket_end for the in-progress bucket. Required — fail-hard
        contract: the caller (ConsumptionProfile.to_view /
        AdjustedPvForecast.to_profile) computed it via
        `Bucket.live_remaining_kwh` before delegating here. When `now`
        falls outside the 7:00..12:30 window no in-progress bucket
        exists and `live_remaining_kwh` is unused.
        """
        new: dict[Bucket, float] = {}
        for bucket, full_kwh in self.by_bucket.items():
            if bucket.is_closed_at(now):
                new[bucket] = 0.0
            elif bucket.is_future_at(now):
                new[bucket] = full_kwh
            else:
                new[bucket] = live_remaining_kwh
        return Buckets(by_bucket=new)
