"""Bucket math — 30-min bucket vocabulary for the 7:00..12:30 PV window.

Holds the `Bucket` value object (identity = (hour, minute) on the
30-min grid) plus operations that work on buckets:

- Time math: `Bucket.is_closed_at`, `is_in_progress_at`, `is_future_at`,
  `remaining_sec_at`, `elapsed_sec_at`, `Bucket.enclosing(now)`.
- kWh math: `live_remaining_kwh(now, pv_w)` (energy from now to end of
  enclosing bucket at constant power), `full_bucket_kwh(now, pv_w,
  so_far)` (realized + extrapolated).
- Structural transform: `buckets_from_now(buckets, *, now,
  live_remaining_kwh)` — closed → 0, in-progress → live override,
  future → unchanged.

This is the shared source of truth for the in-progress bucket arithmetic
consumed by:
- `ConsumptionProfile.to_view` / `AdjustedPvForecast.to_profile` —
  builds the in-progress kWh going into `calculate_target_soc`.
- `AdjustedPvForecast.with_now_aware_in_progress*` — chart display
  (in-progress period rescaled to `full_bucket_kwh × 2` rate).
- `pv_forecast_extrapolation._compute_*_score` — realized rate input
  to the strategy score (full_bucket_kwh × 2 as kWh/h).
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
    clock value use `Bucket.enclosing(now)`.
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


def remaining_sec_in_current_bucket(now: datetime) -> float:
    """Seconds left until the end of the 30-min bucket enclosing `now`.

    Independent of date / window — for `now=09:13:42`, returns 1038.
    Microseconds preserved. Equivalent to `Bucket.enclosing(now).remaining_sec_at(now)`.
    """
    return Bucket.enclosing(now).remaining_sec_at(now)


def live_remaining_kwh(now: datetime, pv_power_w: float) -> float:
    """Energy contribution from `now` to end of the in-progress 30-min bucket.

    Assumes `pv_power_w` (typically `sensor.pv_power_avg_5_minutes`) holds
    constant for the remaining time. Returns kWh.

    Same value consumed by:
    - `AdjustedPvForecast.to_profile` (in-progress bucket → live remaining only,
      past → 0, future → forecast), feeding `calculate_target_soc`.
    - `AdjustedPvForecast.with_now_aware_in_progress` (chart display,
      combined with `pv_bucket_so_far_kwh`).
    - `pv_forecast_extrapolation._compute_*_score` (current bucket realized
      rate = `(so_far + this) × 2`).
    """
    return (pv_power_w / 1000.0) * remaining_sec_in_current_bucket(now) / 3600.0


def full_bucket_kwh(
    now: datetime,
    pv_power_w: float,
    pv_bucket_so_far_kwh: float,
) -> float:
    """Total in-progress bucket kWh = realized so-far + extrapolated remaining.

    The "what we expect this bucket to deliver" estimate, shared by chart
    display (rescaled to kWh/h rate via ×2) and strategy score input.
    """
    return pv_bucket_so_far_kwh + live_remaining_kwh(now, pv_power_w)


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
    it before delegating. When `now` falls outside the 7:00..12:30 window
    no in-progress bucket exists and `live_remaining_kwh` is unused.
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
