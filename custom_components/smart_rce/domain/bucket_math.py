"""Bucket math — 30-min bucket vocabulary for the 7:00..12:30 PV window.

Holds the `Bucket` value object (identity = (hour, minute) on the
30-min grid) plus operations that work on buckets:

- Time math: `Bucket.is_closed_at`, `is_in_progress_at`, `is_future_at`,
  `remaining_sec_at`, `elapsed_sec_at`, `Bucket.enclosing(now)`.
- kWh math (staticmethods, parameterized by `now`): `Bucket.live_remaining_kwh`,
  `Bucket.full_bucket_kwh`. The formula is identical for PV and consumption
  power — both VOs (AdjustedPvForecast and ConsumptionProfile) delegate
  here so the in-progress integration lives in one place.
- Structural transform: `buckets_from_now(buckets, *, now, live_kwh)` —
  closed → 0, in-progress → live override, future → unchanged. Used by
  profile transforms; stays module-level until Phase 2 introduces a
  `BucketProfile` wrapper.

Shared source of truth for the in-progress bucket arithmetic consumed by:
- `ConsumptionProfile.to_view` / `AdjustedPvForecast.to_profile`
- `AdjustedPvForecast.with_now_aware_in_progress*` (chart display)
- `pv_forecast_extrapolation._compute_*_score` (realized rate input)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta


@dataclass(frozen=True)
class Bucket:
    """A 30-min interval on the 7-13 PV-window grid. Identity = (hour, minute).

    Holds only its position in time and the time-arithmetic that follows
    from it. Energy values (kWh, kWh/h rates) live in other types
    (`AdjustedPeriod`, profile dicts) — `Bucket` does not own values.

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

    def start_datetime(self, on: datetime) -> datetime:
        """Bucket start as a tz-aware datetime on `on`'s date + tzinfo."""
        return datetime.combine(
            on.date(), time(hour=self.hour, minute=self.minute), tzinfo=on.tzinfo
        )

    def end_datetime(self, on: datetime) -> datetime:
        """Bucket end (= start + 30 min)."""
        return self.start_datetime(on) + timedelta(minutes=30)

    def is_closed_at(self, now: datetime) -> bool:
        """Return True when `now` is at or past the bucket's end."""
        return now >= self.end_datetime(now)

    def is_in_progress_at(self, now: datetime) -> bool:
        """Return True when bucket_start <= now < bucket_end."""
        start = self.start_datetime(now)
        return start <= now < start + timedelta(minutes=30)

    def is_future_at(self, now: datetime) -> bool:
        """Return True when bucket_start > now (entirely ahead of `now`)."""
        return now < self.start_datetime(now)

    def remaining_sec_at(self, now: datetime) -> float:
        """Seconds from `now` to bucket_end. Negative if `now` past bucket_end.

        For `now` before bucket_start returns the full 1800s (caller
        typically uses this on the enclosing bucket where it lies in
        [0, 1800]).
        """
        return (self.end_datetime(now) - now).total_seconds()

    def elapsed_sec_at(self, now: datetime) -> float:
        """Seconds from bucket_start to `now`. Negative if `now` before start."""
        return (now - self.start_datetime(now)).total_seconds()

    @staticmethod
    def live_remaining_kwh(now: datetime, power_w: float) -> float:
        """Energy contribution from `now` to end of the enclosing 30-min bucket.

        Assumes `power_w` (PV or consumption — formula is symmetric)
        holds constant for the remaining time. Returns kWh.

        Single source of truth for the in-progress bucket integration
        consumed by:
        - `AdjustedPvForecast.to_profile` (PV side, in-progress = remaining
          only; past contributes 0 to the forward-looking deficit).
        - `ConsumptionProfile.to_view` (consumption side, same shape).
        - `Bucket.full_bucket_kwh` (chart display, combined with so_far).
        - `pv_forecast_extrapolation._compute_*_score` indirectly via
          `Bucket.full_bucket_kwh` (realized rate = full_bucket × 2).
        """
        remaining_sec = Bucket.enclosing(now).remaining_sec_at(now)
        return (power_w / 1000.0) * remaining_sec / 3600.0

    @staticmethod
    def full_bucket_kwh(
        now: datetime,
        power_w: float,
        bucket_so_far_kwh: float,
    ) -> float:
        """Total in-progress bucket kWh = realized so-far + extrapolated remaining.

        The "what we expect this bucket to deliver" estimate. Shared by
        chart display (rescaled to kWh/h rate via × 2 inside
        `AdjustedPvForecast.with_now_aware_in_progress*`) and strategy
        score input in extrapolation variants.
        """
        return bucket_so_far_kwh + Bucket.live_remaining_kwh(now, power_w)


def buckets_from_now(
    buckets: dict[tuple[int, int], float],
    *,
    now: datetime,
    live_remaining_kwh: float,
) -> dict[tuple[int, int], float]:
    """Project a 12-bucket forecast snapshot onto the "from-now" view.

    Per bucket (classification by `Bucket.is_*_at(now)`):
    - closed    → 0.0
    - in-progress → `live_remaining_kwh`
    - future    → full_kwh (unchanged)

    `live_remaining_kwh` is the kWh contribution from `now` to bucket_end
    for the in-progress bucket. Required — fail-hard contract: the caller
    (ConsumptionProfile.to_view / AdjustedPvForecast.to_profile) computed
    it via `Bucket.live_remaining_kwh` before delegating here. When `now`
    falls outside the 7:00..12:30 window no in-progress bucket exists
    and `live_remaining_kwh` is unused.
    """
    new_buckets: dict[tuple[int, int], float] = {}
    for (h, m), full_kwh in buckets.items():
        bucket = Bucket(h, m)
        if bucket.is_closed_at(now):
            new_buckets[(h, m)] = 0.0
        elif bucket.is_future_at(now):
            new_buckets[(h, m)] = full_kwh
        else:
            new_buckets[(h, m)] = live_remaining_kwh
    return new_buckets
