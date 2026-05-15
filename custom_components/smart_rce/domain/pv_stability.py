"""PV derivative stability — entity tracking run-length of stable rise.

Holds the cumulative state behind the derivative-aware PV projection
gate (Phase B of the in-progress bucket projection roadmap). Updated
per-minute by the application service from the canonical
`sensor.pv_power_derivative_avg_2min_stability_5m` rolling stddev
reading + the matching derivative value.

`run_start` is the only cumulative state that needs to survive HA
restart — the rest (`last_derivative_w_per_min`, `last_stability_value`,
`last_update`) is transient diagnostic state refreshed on the next minute
tick after boot. `to_dict` / `from_dict` snapshot exposes only
`run_start`, so the storage adapter (`PvStabilityPersistence`) writes
disk only on real state transitions — typically 2-4× per day.

The decision rule that consumes this state (whether to project with a
derivative ramp vs. constant power) lives in Phase C call sites and
reads `is_stable()` + `run_length_sec_at(now)` against the trust
threshold (`PV_STABILITY_MIN_RUN_LENGTH_SEC`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

# Rolling-stddev threshold (W/min) below which we consider the PV
# derivative "stable". Analysis (home-assistant-ops/pv_history/) shows
# 5-min rolling stddev of `derivative(pv_power_avg_2_minutes)`:
# - clear days: p95 around 9.9 W/min
# - variable days: median ~40 W/min
# A 15 W/min cut catches 95% of clear-day stability with minimal
# false-positive on variable days.
PV_STABILITY_THRESHOLD_W_PER_MIN: Final[float] = 15.0

# Minimum continuous stable run required before we trust the derivative
# for ramp projection. 5 min = full rolling-stddev window seen with a
# settled value below threshold; below that we're inside the warm-up
# region where the stddev sensor itself may not yet be reliable.
PV_STABILITY_MIN_RUN_LENGTH_SEC: Final[float] = 5 * 60.0


@dataclass
class PvStability:
    """Tracks the run-length of consecutive "stable" derivative samples.

    `run_start` is set when the stability sensor crosses below
    `PV_STABILITY_THRESHOLD_W_PER_MIN`, cleared when it crosses back
    above. `run_length_sec_at(now)` returns elapsed seconds since the
    transition.

    Transient (not persisted) fields hold the latest sensor readings for
    diagnostic sensors and projection logic — they're refreshed by the
    next `update()` call after HA boot, so leaving them None across
    restart costs ~one minute tick of staleness.
    """

    # Persisted — cumulative state needed across HA restart.
    run_start: datetime | None = None
    # Transient — latest sensor readings; refreshed every minute.
    last_derivative_w_per_min: float | None = None
    last_stability_value: float | None = None
    last_update: datetime | None = None

    def update(
        self,
        now: datetime,
        derivative_w_per_min: float | None,
        stability_value: float | None,
    ) -> None:
        """Apply latest sensor readings, maintain `run_start` transition."""
        self.last_derivative_w_per_min = derivative_w_per_min
        self.last_stability_value = stability_value
        self.last_update = now
        is_stable = (
            stability_value is not None
            and stability_value < PV_STABILITY_THRESHOLD_W_PER_MIN
        )
        if is_stable and self.run_start is None:
            self.run_start = now
        elif not is_stable and self.run_start is not None:
            self.run_start = None

    def is_stable(self) -> bool:
        """Return True while a stable run is in progress (run_start set)."""
        return self.run_start is not None

    def run_length_sec_at(self, now: datetime) -> float:
        """Seconds since current stable run began; 0.0 when not stable."""
        if self.run_start is None:
            return 0.0
        return (now - self.run_start).total_seconds()

    # --- Persistence snapshot (cross HA restart) --- #

    def to_dict(self) -> dict[str, Any]:
        """Snapshot only the cumulative state — transients refresh on next tick."""
        return {
            "run_start": self.run_start.isoformat() if self.run_start else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PvStability:
        """Restore from snapshot. Transient fields stay None until next update()."""
        raw = data.get("run_start")
        return cls(
            run_start=datetime.fromisoformat(raw) if raw else None,
        )
