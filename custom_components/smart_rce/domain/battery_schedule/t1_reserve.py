"""T1SocReserve — SoC the battery should hold for the rest of the T1 window.

Value object resolved from one `TargetSoc` variant: the reading for the
per-bucket median consumption of the earlier workdays (decision 14.10.2026 —
replay of 04-14.10: the old max(flat, prev without the worst) overshot by
~5 pp and protected no better than flat). Without consumption history it falls
back to the flat reading. The flat and prev-without-worst readings stay as
components for comparison.

One figure, several readers: before 07:00 it is the night charge target of the
CHARGE_MORNING_AUTO slot; 07:00-13:00 the reserve still needed for the rest of
the window (falls as the morning goes), the line a morning discharge must not
cross. Backtest behind the rule: home-assistant-ops
`research/2026-10-11-backtest-poranne-ladowanie/`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ..target_soc import TargetSoc


class ReserveWindow(StrEnum):
    """Which part of the 7-13 window the figure covers."""

    FULL = "full"
    """The whole window ahead — the night before / the evening preview."""

    REMAINING = "remaining"
    """Only what is left of today's window (07:00-13:00, falls over time)."""

    PAST = "past"
    """Today's window replayed in full after it closed (13:00-16:00)."""


@dataclass(frozen=True)
class T1SocReserve:
    """Reserve for the T1 window of `day`, plus what it was made of.

    `source` names the forecast variant the value came from (`live`,
    `tomorrow_live`, or an EXTRAP key) — surfaced in telegrams and on the
    dashboard so a stale or fallback value is visible.
    """

    day: date
    value: int
    flat: int | None
    prev_without_worst: int | None
    dip_kwh: float | None
    source: str
    window: ReserveWindow
    median: int | None = None

    @classmethod
    def from_target_soc(
        cls,
        day: date,
        target_soc: TargetSoc,
        *,
        source: str,
        window: ReserveWindow,
    ) -> T1SocReserve | None:
        """Resolve the reserve from one variant; None when it has nothing yet."""
        flat = target_soc.flat.value if target_soc.flat is not None else None
        prev = cls.prev_without_worst_of(
            r.value for r in target_soc.prev_days if r is not None
        )
        driving = (
            target_soc.median if target_soc.median is not None else target_soc.flat
        )
        if driving is None:
            return None
        return cls(
            day=day,
            value=min(100, driving.value),
            flat=flat,
            prev_without_worst=prev,
            median=target_soc.median.value if target_soc.median is not None else None,
            dip_kwh=driving.dip_kwh,
            source=source,
            window=window,
        )

    @staticmethod
    def prev_without_worst_of(values: Iterable[int]) -> int | None:
        """Highest prev-workday value once the single highest is dropped.

        With one value there is nothing to drop — that value is the answer.
        """
        ordered = sorted(values, reverse=True)
        if not ordered:
            return None
        return ordered[1] if len(ordered) > 1 else ordered[0]

    def same_slot_as(self, other: T1SocReserve | None) -> bool:
        """Tell whether `other` covers the same day, variant and window."""
        return (
            other is not None
            and other.day == self.day
            and other.source == self.source
            and other.window == self.window
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "value": self.value,
            "flat": self.flat,
            "prev_without_worst": self.prev_without_worst,
            "median": self.median,
            "dip_kwh": self.dip_kwh,
            "source": self.source,
            "window": self.window.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> T1SocReserve | None:
        try:
            return cls(
                day=date.fromisoformat(data["day"]),
                value=int(data["value"]),
                flat=data.get("flat"),
                prev_without_worst=data.get("prev_without_worst"),
                median=data.get("median"),
                dip_kwh=data.get("dip_kwh"),
                source=str(data.get("source", "")),
                window=ReserveWindow(data.get("window", ReserveWindow.FULL.value)),
            )
        except (KeyError, TypeError, ValueError):
            return None
