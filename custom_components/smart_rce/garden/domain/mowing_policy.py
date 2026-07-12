"""Mowing policy — user-tunable planner thresholds (persisted).

`MowingPolicy` is the garden-owned aggregate holding the mowing planner's
tunable policy. v1: `fresh_start_battery` — the SoC threshold above which a
fresh (progress == 0) program is dispatched. Persisted via
`MowingPolicyRepository` (Store) so a tuned value survives restarts —
consistently with `RainState.dry_hours` (both domain policies persist via Store,
unlike pure UI-input numbers which use RestoreNumber).

A DDD entity (mutable + persisted), so a plain class, not a dataclass.
"""

from __future__ import annotations

from typing import Any

# Mirrors MowingPlanner.DEFAULT_FRESH_BATTERY (a full-ish charge banks a long
# stretch before the first dispatch).
_DEFAULT_FRESH_BATTERY = 90


class MowingPolicy:
    """Mutable aggregate — tunable mowing planner policy, persisted via repo."""

    def __init__(self, fresh_start_battery: int = _DEFAULT_FRESH_BATTERY) -> None:
        self.fresh_start_battery = fresh_start_battery

    def set_fresh_start_battery(self, value: int) -> bool:
        """Set the fresh-start SoC threshold. Returns True if it changed."""
        if value == self.fresh_start_battery:
            return False
        self.fresh_start_battery = value
        return True

    def to_dict(self) -> dict[str, Any]:
        return {"fresh_start_battery": self.fresh_start_battery}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MowingPolicy:
        value = data.get("fresh_start_battery", _DEFAULT_FRESH_BATTERY)
        return cls(fresh_start_battery=int(value))
