"""MorningChargeTarget — the SoC the morning auto slot charges to.

Value object resolved from one `TargetSoc` variant: the larger of the flat
reading (default consumption) and the prev-workday readings without the single
worst one. Dropping one outlier keeps a freak day (guests, oven, a sick day at
home) from dictating the charge, while the next-worst still counts.

Backtest behind the rule: `research/2026-10-11-backtest-poranne-ladowanie/`
in home-assistant-ops (shortfall on 2.6% of winter workdays, 1.7-2.5% summer).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ..target_soc import TargetSoc


@dataclass(frozen=True)
class MorningChargeTarget:
    """Charge target for the morning of `day`, plus what it was made of.

    `source` names the forecast variant the value came from (`live` after
    midnight, `tomorrow_live` the evening before) — surfaced in telegrams and
    the dashboard so a stale or fallback value is visible.
    """

    day: date
    value: int
    flat: int | None
    prev_without_worst: int | None
    source: str

    @classmethod
    def from_target_soc(
        cls, day: date, target_soc: TargetSoc, *, source: str
    ) -> MorningChargeTarget | None:
        """Resolve the target from one variant; None when it has nothing yet."""
        flat = target_soc.flat.value if target_soc.flat is not None else None
        prev = cls.prev_without_worst_of(
            r.value for r in target_soc.prev_days if r is not None
        )
        candidates = [v for v in (flat, prev) if v is not None]
        if not candidates:
            return None
        return cls(
            day=day,
            value=min(100, max(candidates)),
            flat=flat,
            prev_without_worst=prev,
            source=source,
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "value": self.value,
            "flat": self.flat,
            "prev_without_worst": self.prev_without_worst,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MorningChargeTarget | None:
        try:
            return cls(
                day=date.fromisoformat(data["day"]),
                value=int(data["value"]),
                flat=data.get("flat"),
                prev_without_worst=data.get("prev_without_worst"),
                source=str(data.get("source", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None
