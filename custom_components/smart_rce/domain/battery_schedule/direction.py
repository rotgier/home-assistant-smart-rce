"""Battery flow direction + SoC-zone rate model (leaf foundation types)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..ems_operation import EmsMode


@dataclass(frozen=True)
class RateZone:
    """SoC range with associated rate (seconds to traverse 1 pp).

    Zone covers `[soc_from, soc_to)` (half-open — `soc_to` exclusive).
    Used to model non-linear inverter behavior: BMS-compressed mid-range
    (25→16% discharges fast as pp represent less energy), calibration
    pauses (16→14%), and post-calibration fast end (14→10%). See
    `research/2026-05-04-battery-discharge-per-pp.csv` for empirical
    data.
    """

    soc_from: float
    soc_to: float
    sec_per_pp: float

    def overlap_seconds(self, low: float, high: float) -> float:
        """Seconds contributed by this zone for SoC traversal `[low, high)`.

        Returns 0 when the zone doesn't overlap the requested range.
        """
        ol = max(low, self.soc_from)
        oh = min(high, self.soc_to)
        return (oh - ol) * self.sec_per_pp if oh > ol else 0.0


class Direction(Enum):
    """Battery flow direction (DISCHARGE / CHARGE) + per-direction settings.

    Enum with bound metadata: EMS mode, power limit, charge-toggle requirement,
    SoC-zone rate model. Every `SlotKind` references one via `SlotProfile`.

    DISCHARGE rate zones — empirical from 2026-05-04 morning discharge session
    (DISCHARGE_BATTERY @ 6kW, BMS ~5kW effective). FAST ZONE 25-100 covers
    normal evening discharge 100→30; below 25 we hit BMS quirks (compressed
    mid-range, calibration pause 14-16, fast end below 14).

    CHARGE rate zones — empirical from nine slot-driven sessions 22.09-02.10
    2026 (`research/2026-10-02-battery-charge-per-pp.csv`). Flat 75 sec/pp
    holds remarkably well up to 89%, then the BMS plateaus across 89-91 and
    runs a compressed, uneven top. Modelling the top as one zone rather than
    per-pp is deliberate: the plateau's exact position wanders between
    sessions while the time to cross it does not.

    Comparison: NEVER use `is` across `live_reload()` boundary (re-imported
    enum class gives new member identity). Use `direction.is_discharge` /
    `direction.is_charge` (name-based) for live_reload safety. Within a
    single import lifetime, `direction is Direction.DISCHARGE` is fine.
    """

    DISCHARGE = (
        # PV+battery hybrid. Morning: PV covers load, battery supplies overflow.
        # Evening: PV is zero, mode degrades to battery-only — same effect as
        # DISCHARGE_BATTERY without needing a second EMS mode in the matrix.
        EmsMode.DISCHARGE_PV,
        6000,
        False,
        (
            RateZone(soc_from=25.0, soc_to=100.01, sec_per_pp=75.0),
            RateZone(soc_from=16.0, soc_to=25.0, sec_per_pp=36.0),
            RateZone(soc_from=14.0, soc_to=16.0, sec_per_pp=97.0),
            RateZone(soc_from=0.0, soc_to=14.0, sec_per_pp=34.0),
        ),
    )
    CHARGE = (
        EmsMode.CHARGE_BATTERY,
        6000,
        True,
        (
            # Flat and tight: median 75 s/pp with every bucket inside 72-80,
            # over 59 consecutive points in the longest session.
            RateZone(soc_from=0.0, soc_to=89.0, sec_per_pp=75.0),
            # BMS plateau. Which of the two points stalls varies wildly
            # (37-685 s on a single pp), but crossing both takes 630-820 s
            # in every session, so the pair is modelled as one zone.
            RateZone(soc_from=89.0, soc_to=91.0, sec_per_pp=390.0),
            # Compressed top: fast 91-94, then a long taper into 100 where
            # the last point alone costs 200-580 s.
            RateZone(soc_from=91.0, soc_to=100.01, sec_per_pp=115.0),
        ),
    )

    def __init__(
        self,
        ems_mode: EmsMode,
        power_limit_w: int,
        needs_charge_toggle: bool,
        rate_zones: tuple[RateZone, ...],
    ) -> None:
        self.ems_mode = ems_mode
        self.power_limit_w = power_limit_w
        self.needs_charge_toggle = needs_charge_toggle
        self.rate_zones = rate_zones

    @property
    def is_discharge(self) -> bool:
        return self.name == "DISCHARGE"

    @property
    def is_charge(self) -> bool:
        return self.name == "CHARGE"

    def seconds_for_soc_traversal(self, start_soc: float, end_soc: float) -> float:
        """Sum sec_per_pp across rate zones covering the SoC traversal start→end.

        Direction-agnostic — internally normalizes to [low, high] so callers
        can pass `(current_soc, target_soc)` regardless of charge/discharge.
        Returns 0 when start == end. SoC outside zone coverage contributes 0.
        """
        low, high = min(start_soc, end_soc), max(start_soc, end_soc)
        if low >= high:
            return 0.0
        return sum(z.overlap_seconds(low, high) for z in self.rate_zones)
