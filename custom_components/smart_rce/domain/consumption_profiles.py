"""Consumption profile vocabulary — VO, dual-anchor entity, fetch port.

Moved out of `pv_forecast.py` so the `ConsumptionProfiles` rich entity
can reference `ConsumptionProfile` (its element VO) without creating a
circular import with `PvForecast` (which now holds a `ConsumptionProfiles`
field). `pv_forecast.py` re-exports `ConsumptionProfile` /
`PREV_DAYS_COUNT` for back-compat with existing callers.

Single concept per file:
- `ConsumptionProfile` — immutable per-day profile (12 buckets 7:00..12:30)
- `ConsumptionProfiles` — dual-anchor snapshot held by the PvForecast
  aggregate; carries refresh + retry behavior (rich domain model)
- `ConsumptionProfileSource` — port for the recorder-backed fetcher;
  infrastructure provides the impl
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import ClassVar, Final, Protocol

from .target_soc import CONSUMPTION_PER_30MIN

# --- Constants --- #

# How many prev-workday baselines to fetch. 8 covers a full work-week
# (~5 days) plus another 3 for context on baseline volatility.
PREV_DAYS_COUNT: Final[int] = 8

# Exactly 12 buckets covering 7:00..12:30 in 30-min steps — strict
# contract enforced by `ConsumptionProfile.__post_init__`.
_EXPECTED_BUCKETS: Final[frozenset[tuple[int, int]]] = frozenset(
    (h, m) for h in range(7, 13) for m in (0, 30)
)

# 5 attempts × 60s interval = ~5 minutes worst-case to recover from a
# startup race (workday calendar or recorder not yet ready). After cap,
# stop hammering the recorder — next scheduled trigger (bucket boundary
# at :00/:30, or daily refresh at 05:55) gets a fresh chance.
MAX_RETRIES: Final[int] = 5


# --- Value object --- #


@dataclass(frozen=True)
class ConsumptionProfile:
    """Consumption per 30-min bucket, keyed by (hour, minute) -> kWh.

    Strict contract: `buckets` must contain exactly 12 entries covering
    7:00..12:30 in 30-min steps. Missing buckets are filled with
    `CONSUMPTION_PER_30MIN` at the infra↔domain boundary
    (`ConsumptionProfileLoader._bucket_profiles_by_date`) so callers can
    rely on `.get(h, m)` returning a non-None float.
    """

    buckets: dict[tuple[int, int], float]
    source_date: date | None = None  # workday the profile was taken from

    def __post_init__(self) -> None:
        got = frozenset(self.buckets.keys())
        if got != _EXPECTED_BUCKETS:
            missing = sorted(_EXPECTED_BUCKETS - got)
            extra = sorted(got - _EXPECTED_BUCKETS)
            raise ValueError(
                "ConsumptionProfile must have exactly 12 buckets 7:00..12:30; "
                f"missing={missing}, extra={extra}"
            )

    def get(self, hour: int, minute: int) -> float:
        return self.buckets[(hour, minute)]

    @classmethod
    def flat(
        cls,
        value: float = CONSUMPTION_PER_30MIN,
        source_date: date | None = None,
    ) -> ConsumptionProfile:
        """Synthetic flat baseline — every bucket = `value` kWh."""
        return cls(
            buckets={(h, m): value for h, m in _EXPECTED_BUCKETS},
            source_date=source_date,
        )


# --- Port (Protocol) --- #


class ConsumptionProfileSource(Protocol):
    """Port for fetching prev-workday consumption profiles by anchor date.

    Implemented in infrastructure by `ConsumptionProfileLoader`.
    Returns a list of length `count` — each entry is a `ConsumptionProfile`
    when the recorder + workday calendar produced data for that walk-back
    workday, or `None` when no data could be assembled (e.g. fewer
    workdays in the window than `count`, or the calendar/recorder wasn't
    ready when fetched).
    """

    async def fetch_for_anchor(
        self, anchor: date, count: int
    ) -> list[ConsumptionProfile | None]: ...


# --- Rich domain entity --- #


@dataclass
class ConsumptionProfiles:
    """Dual-anchor snapshot of prev-workday consumption profiles.

    `today_profiles` — anchor = today's date; `today_profiles[0]` is the
    most recent workday strictly before today (= yesterday if a workday,
    otherwise prior Friday). Used by `target_soc_prev_day_X` sensors.

    `tomorrow_profiles` — anchor = tomorrow's date; `tomorrow_profiles[0]`
    is the most recent workday strictly before tomorrow (= today if a
    workday). Used by `target_soc_tomorrow_prev_day_X` sensors and by
    the target-SOC matrix when its date picker is on tomorrow.

    `failed_attempts` tracks consecutive partial-fetch outcomes since
    the last full success. Reset to 0 on a fully populated refresh.
    Application uses `should_retry()` to decide whether to schedule
    another attempt.
    """

    today_profiles: list[ConsumptionProfile | None] = field(
        default_factory=lambda: [None] * PREV_DAYS_COUNT
    )
    tomorrow_profiles: list[ConsumptionProfile | None] = field(
        default_factory=lambda: [None] * PREV_DAYS_COUNT
    )
    failed_attempts: int = 0

    MAX_RETRIES: ClassVar[int] = MAX_RETRIES

    @classmethod
    def empty(cls) -> ConsumptionProfiles:
        """Initial empty state — every prev_X slot None on both anchors."""
        return cls()

    async def refresh_full(
        self, source: ConsumptionProfileSource, now: datetime
    ) -> None:
        """Fetch both anchors in parallel; update state + retry counter.

        Increments `failed_attempts` on a partial result (any slot left
        `None`); resets to 0 on full success. Caller checks
        `should_retry()` afterward and schedules the next attempt with
        the appropriate timing primitive (e.g. `async_call_later`).
        """
        today_anchor = now.date()
        tomorrow_anchor = today_anchor + timedelta(days=1)
        today, tomorrow = await asyncio.gather(
            source.fetch_for_anchor(today_anchor, PREV_DAYS_COUNT),
            source.fetch_for_anchor(tomorrow_anchor, PREV_DAYS_COUNT),
        )
        self.today_profiles = today
        self.tomorrow_profiles = tomorrow
        self.failed_attempts = self.failed_attempts + 1 if self.is_partial() else 0

    async def refresh_tomorrow_only(
        self, source: ConsumptionProfileSource, now: datetime
    ) -> None:
        """Refresh only the tomorrow-anchored list (intraday today-prev_1 growth).

        Called on bucket boundary inside the PV window (07:30..13:30) —
        today's profile data grows as utility_meter cycles close, so the
        `tomorrow_profiles[0]` slot (= today's profile) needs to be
        re-read. Today-anchored profiles never change during the day
        (they reference historical yesterday + earlier workdays).
        """
        tomorrow_anchor = now.date() + timedelta(days=1)
        self.tomorrow_profiles = await source.fetch_for_anchor(
            tomorrow_anchor, PREV_DAYS_COUNT
        )

    def is_partial(self) -> bool:
        """Any anchor missing at least one profile."""
        return any(p is None for p in self.today_profiles) or any(
            p is None for p in self.tomorrow_profiles
        )

    def is_unavailable(self) -> bool:
        """Both anchors fully empty — likely a fetch failure, not a normal
        partial (e.g. fewer than `PREV_DAYS_COUNT` workdays available).
        """
        return all(p is None for p in self.today_profiles) and all(
            p is None for p in self.tomorrow_profiles
        )

    def should_retry(self) -> bool:
        """Schedule another refresh attempt within the retry budget."""
        return self.is_partial() and self.failed_attempts < self.MAX_RETRIES
