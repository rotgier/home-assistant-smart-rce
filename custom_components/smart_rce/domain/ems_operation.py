"""EmsOperation — value object representing a Goodwe EMS recommendation.

Unified representation of "what should the inverter EMS do right now".
Produced by:
- `GridExportManager.update` — intervention-driven (POSITIVE/NEGATIVE)
- (Etap F) `BatterySchedule` slots — schedule-driven (charge/discharge windows)

Consumed by `GoodweEmsActuator.apply_if_changed(target)` which writes
`select.goodwe_ems_mode` + `number.goodwe_ems_power_limit` via scene.apply.

`source` is diagnostic (drives sensor labels; resolution precedence
between competing sources is handled in `Ems._resolve_ems_operation`).

`ems_mode`/`power_limit_w` are the Goodwe inverter registers; "auto" is
the neutral state — Goodwe ignores power_limit_w when mode=auto.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EmsMode = Literal["auto", "discharge_battery", "charge_battery"]
EmsOperationSource = Literal["neutral", "grid_export", "schedule"]


@dataclass(frozen=True)
class EmsOperation:
    """Goodwe EMS target state — mode + optional power limit + diagnostic."""

    ems_mode: EmsMode
    power_limit_w: int | None
    source: EmsOperationSource = "neutral"
    reason: str | None = None

    @classmethod
    def neutral(cls, reason: str | None = None) -> EmsOperation:
        """No intervention — Goodwe runs its default auto policy."""
        return cls(ems_mode="auto", power_limit_w=None, source="neutral", reason=reason)

    @classmethod
    def from_grid_intervention(
        cls, ems_mode: EmsMode, power_limit_w: int | None, reason: str | None
    ) -> EmsOperation:
        """GridExportManager intervention active — POSITIVE/NEGATIVE recommendation."""
        return cls(
            ems_mode=ems_mode,
            power_limit_w=power_limit_w,
            source="grid_export",
            reason=reason,
        )

    @property
    def is_neutral(self) -> bool:
        """True when mode=auto (no override of Goodwe default behavior)."""
        return self.ems_mode == "auto"

    def matches_inverter(
        self, current_mode: str | None, current_power_limit: int | None
    ) -> bool:
        """Compare target to observed inverter state (state-diff for actuator).

        In auto mode, power_limit_w is ignored on the inverter side
        (Goodwe register 47512 unused) — we normalize the comparison so a
        stale power_limit value on the inverter does not force a re-apply.
        """
        if current_mode != self.ems_mode:
            return False
        if self.ems_mode == "auto":
            return True
        return current_power_limit == self.power_limit_w
