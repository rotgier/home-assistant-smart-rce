"""One-shot operation domain.

Contains `OneShotOperation` + `OneShotParams` + `OneShotDisengageReason`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum
from typing import Any

from ..ems_operation import EmsOperation
from .direction import Direction
from .entry import NotificationLevel
from .operation import BatteryOperation


class OneShotDisengageReason(StrEnum):
    """Why a one-shot operation stopped — used by `OneShotEnded` event."""

    TARGET_REACHED = "target_reached"
    """SoC reached target (normal completion)."""

    EXPIRED = "expired"
    """`now >= end_at` reached — window timeout."""

    CANCELLED = "cancelled"
    """User pressed Cancel button."""


@dataclass(frozen=True)
class OneShotOperation:
    """Active ad-hoc battery operation overriding scheduled slots.

    Created when user presses "Execute" — lives in `BatterySchedule._oneshot`
    until target_reached/expired (auto-clear in compute_operation) or
    cancelled (user button). Precedence #0 — beats every scheduled slot.

    Uses absolute datetimes (not time-of-day) so it handles cross-midnight
    cleanly: user can set end_time=06:00 at 22:00 today, aggregate combines
    into tomorrow 06:00 when creating this VO.
    """

    direction: Direction
    target_soc: float
    end_at: datetime
    started_at: datetime
    # Always NORMAL — deliberate user action, voice escalation at arbitrary
    # hours is disruptive. Not configurable in UI. If EMERGENCY semantics
    # needed for evening peak, use scheduled slot DISCHARGE_EVENING (where
    # SlotProfile carries notification_level=EMERGENCY).
    notification_level: NotificationLevel = NotificationLevel.NORMAL

    def __post_init__(self) -> None:
        if not 0.0 <= self.target_soc <= 100.0:
            raise ValueError(f"target_soc {self.target_soc} outside [0, 100]")
        if self.end_at <= self.started_at:
            raise ValueError(
                f"end_at {self.end_at} must be after started_at {self.started_at}"
            )

    def is_expired(self, now: datetime) -> bool:
        return now >= self.end_at

    def target_reached(self, current_soc: float) -> bool:
        if self.direction.is_discharge:
            return current_soc <= self.target_soc
        return current_soc >= self.target_soc

    def to_battery_operation(self) -> BatteryOperation:
        """Build BatteryOperation (output) from this active one-shot.

        Symmetric with `BatteryScheduleEntry.to_battery_operation` — keeps
        the "how a source translates to BatteryOperation" logic with the
        source itself (Tell-Don't-Ask), not on BatteryOperation.
        """
        d = self.direction
        return BatteryOperation(
            ems_op=EmsOperation(
                ems_mode=d.ems_mode,
                power_limit_w=d.power_limit_w,
                source="schedule",
                reason=f"oneshot={d.name}",
            ),
            needs_charge_toggle=d.needs_charge_toggle,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction.name,
            "target_soc": self.target_soc,
            "end_at": self.end_at.isoformat(),
            "started_at": self.started_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OneShotOperation | None:
        try:
            return cls(
                direction=Direction[data["direction"]],
                target_soc=float(data["target_soc"]),
                end_at=datetime.fromisoformat(data["end_at"]),
                started_at=datetime.fromisoformat(data["started_at"]),
            )
        except (KeyError, ValueError, TypeError):
            return None


@dataclass(frozen=True)
class OneShotParams:
    """User-editable defaults for one-shot operations (per direction).

    `end_time` is time-of-day; aggregate combines it with current date when
    starting a one-shot. If end_time <= now.time(), aggregate rolls to next
    day (e.g. discharge until 06:00 started at 22:00 ends tomorrow 06:00).
    """

    target_soc: float
    end_time: time

    def __post_init__(self) -> None:
        if not 0.0 <= self.target_soc <= 100.0:
            raise ValueError(f"target_soc {self.target_soc} outside [0, 100]")

    def with_target_soc(self, value: float) -> OneShotParams:
        return dataclasses.replace(self, target_soc=value)

    def with_end_time(self, value: time) -> OneShotParams:
        return dataclasses.replace(self, end_time=value)

    @classmethod
    def defaults_by_direction(cls) -> dict[Direction, OneShotParams]:
        """Build default params per direction — used by aggregate's field factory.

        DISCHARGE: end at 22:00 with target SoC 10% (evening peak default).
        CHARGE: end at 06:00 with target SoC 100% (overnight cheap-rate fill).
        """
        return {
            Direction.DISCHARGE: cls(target_soc=10.0, end_time=time(22, 0)),
            Direction.CHARGE: cls(target_soc=100.0, end_time=time(6, 0)),
        }

    @classmethod
    def restore_by_direction(
        cls, data: dict[str, Any]
    ) -> dict[Direction, OneShotParams]:
        """Restore params dict from persisted state with backward compat.

        Preferred format (current): nested under "oneshot_params" keyed by
        `Direction.name`. Falls back to legacy flat keys
        ("discharge_oneshot_params" / "charge_oneshot_params") from
        pre-dict-refactor deploys. Used by `BatterySchedule.from_dict`.
        """
        defaults = cls.defaults_by_direction()
        nested = data.get("oneshot_params")
        if nested:
            return {
                Direction.DISCHARGE: cls.from_dict(
                    nested.get("DISCHARGE", {}),
                    default=defaults[Direction.DISCHARGE],
                ),
                Direction.CHARGE: cls.from_dict(
                    nested.get("CHARGE", {}),
                    default=defaults[Direction.CHARGE],
                ),
            }
        # Legacy flat keys — restore from pre-refactor format if present.
        return {
            Direction.DISCHARGE: cls.from_dict(
                data.get("discharge_oneshot_params", {}),
                default=defaults[Direction.DISCHARGE],
            ),
            Direction.CHARGE: cls.from_dict(
                data.get("charge_oneshot_params", {}),
                default=defaults[Direction.CHARGE],
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_soc": self.target_soc,
            "end_time": self.end_time.isoformat(timespec="minutes"),
        }

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], *, default: OneShotParams
    ) -> OneShotParams:
        try:
            return cls(
                target_soc=float(data.get("target_soc", default.target_soc)),
                end_time=time.fromisoformat(
                    data.get("end_time", default.end_time.isoformat(timespec="minutes"))
                ),
            )
        except (ValueError, TypeError):
            return default
