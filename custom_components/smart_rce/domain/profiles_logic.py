"""Profile bucket math — structural now-aware transforms.

Pure helpers operating on the 12-bucket dict shape shared by `PvProfile`
and `ConsumptionProfile` (`{(hour, minute): kWh}` covering 7:00..12:30).
No coupling to the VO types — both VOs delegate to these helpers from
`to_view()` / `to_profile()`, after they have computed the per-bucket
live override (their integration logic stays close to the data source).

The helpers do NOT integrate power over time — callers (the VO methods)
own that step, because:
- ConsumptionProfile uses a simple `live_consumption_w x remaining_sec /
  3600` integration that is unlikely to change.
- AdjustedPvForecast will eventually grow derivative-aware projection
  for stable-clear-sky bucket growth — the integration formula
  diverges. But the structural placement (closed=0, in-progress=live,
  future=unchanged) stays identical and lives here.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta


def buckets_from_now(
    buckets: dict[tuple[int, int], float],
    *,
    now: datetime,
    live_remaining_kwh: float,
) -> dict[tuple[int, int], float]:
    """Project a 12-bucket forecast snapshot onto the "from-now" view.

    Per bucket:
    - bucket_end <= now (closed)            → 0.0
    - bucket_start <= now < bucket_end      → `live_remaining_kwh`
    - now < bucket_start (future)           → full_kwh (unchanged)

    `live_remaining_kwh` is the kWh contribution from `now` to bucket_end
    for the in-progress bucket. Required — fail-hard contract: the caller
    (ConsumptionProfile.to_view / AdjustedPvForecast.to_profile) computed
    it before delegating here. When `now` falls outside the 7:00..12:30
    window no in-progress bucket exists and `live_remaining_kwh` is
    unused, but the parameter stays required so the API remains explicit.
    """
    new_buckets: dict[tuple[int, int], float] = {}
    for (h, m), full_kwh in buckets.items():
        bucket_start = datetime.combine(
            now.date(), time(hour=h, minute=m), tzinfo=now.tzinfo
        )
        bucket_end = bucket_start + timedelta(minutes=30)
        if now >= bucket_end:
            new_buckets[(h, m)] = 0.0
        elif now < bucket_start:
            new_buckets[(h, m)] = full_kwh
        else:
            new_buckets[(h, m)] = live_remaining_kwh
    return new_buckets


def remaining_sec_in_current_bucket(now: datetime) -> float:
    """Seconds left until the end of the 30-min bucket enclosing `now`.

    Independent of date / window — for `now=09:13:42`, returns
    `30 * 60 - 13*60 - 42 = 1038`. When `now` is mid-second the
    microseconds component is preserved.
    """
    elapsed_sec = (now.minute % 30) * 60 + now.second + now.microsecond / 1_000_000.0
    return 1800.0 - elapsed_sec
