"""SlotSession / UpcomingRun — what a slot did today and what it is about to do.

Both are read models for the dashboard: the price chart shades each session
with the level the battery actually stopped at, and each run still ahead with
the target it heads for. Neither steers anything.

Timestamps go out to the second: the dashboard parses them in the browser,
and microsecond fractions are not something every JS engine accepts.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from .entry import DisengageReason, SlotKind


@dataclass(frozen=True)
class SlotSession:
    """One engagement of a scheduled slot, from engage to disengage.

    Open while `ended_at` is None. `end_soc` is the SoC at the tick the slot
    let go — the level reached, not the plan — and `target_soc` the target in
    force at that moment (auto slots recompute theirs during the session).
    """

    slot: SlotKind
    started_at: datetime
    start_soc: float
    target_soc: float | None
    ended_at: datetime | None = None
    end_soc: float | None = None
    reason: DisengageReason | None = None

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    def closed(
        self,
        at: datetime,
        soc: float,
        target_soc: float | None,
        reason: DisengageReason,
    ) -> SlotSession:
        return replace(
            self, ended_at=at, end_soc=soc, target_soc=target_soc, reason=reason
        )

    def with_target(self, target_soc: float | None) -> SlotSession:
        return replace(self, target_soc=target_soc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot.name,
            "start": self.started_at.isoformat(timespec="seconds"),
            "start_soc": self.start_soc,
            "target": self.target_soc,
            "end": self.ended_at.isoformat(timespec="seconds")
            if self.ended_at is not None
            else None,
            "end_soc": self.end_soc,
            "reason": self.reason.value if self.reason is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SlotSession | None:
        """Restore a stored session; None when the record is unreadable."""
        try:
            return cls(
                slot=SlotKind[data["slot"]],
                started_at=datetime.fromisoformat(data["start"]),
                start_soc=float(data["start_soc"]),
                target_soc=(
                    float(data["target"]) if data.get("target") is not None else None
                ),
                ended_at=(
                    datetime.fromisoformat(data["end"]) if data.get("end") else None
                ),
                end_soc=(
                    float(data["end_soc"]) if data.get("end_soc") is not None else None
                ),
                reason=(
                    DisengageReason(data["reason"]) if data.get("reason") else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class UpcomingRun:
    """A run of an auto slot still ahead today.

    `start` is when DELAYED_TO_END is expected to engage — the window end less
    the time the rate model needs plus the kind's lead — never before the
    window opens.
    """

    slot: SlotKind
    window_start: datetime
    start: datetime
    end: datetime
    start_soc: float
    target_soc: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot.name,
            "window_start": self.window_start.isoformat(timespec="seconds"),
            "start": self.start.isoformat(timespec="seconds"),
            "end": self.end.isoformat(timespec="seconds"),
            "start_soc": self.start_soc,
            "target": self.target_soc,
        }
