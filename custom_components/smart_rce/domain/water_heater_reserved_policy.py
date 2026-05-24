"""WaterHeaterReservedPolicy — persisted state for reserved-power value (W).

Aggregate root for water heater reservation power (`reserved` in
`WaterHeaterManager._balanced_target` for `battery_charge_limit > 7`).
Two modes:
- AUTO: `compute_current_value(now, input)` runs the stub (currently
  returns 3000 W; TODO: dynamic logic based on RCE prices + PV forecast
  + weather summary)
- MANUAL: `compute_current_value` short-circuits and returns `manual_value`
  set by user via UI (NumberEntity)

Persisted state is minimal — only `mode` + `manual_value`. No in-memory
cache needed — the computation is pure and inputs come per-tick from Ems.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 — used in compute_current_value signature
from enum import StrEnum
from typing import Any


class ReservedMode(StrEnum):
    """Source of `reserved` value.

    AUTO — `compute_current_value` runs the computation (currently a stub).
    MANUAL — `compute_current_value` short-circuits to `manual_value` set
    via NumberEntity.
    """

    AUTO = "AUTO"
    MANUAL = "MANUAL"


@dataclass(frozen=True)
class WaterHeaterReservedInput:
    """Inputs for compute_current_value. All fields optional — None = unavailable.

    Ems.update_state aggregates these from existing collaborators
    (RcePrices coordinator, pv_forecast_service, weather_listener) and
    passes the snapshot to `WaterHeaterReservedService.update`.
    """

    rce_today: list[float] | None = None
    pv_forecast_today: list[float] | None = None
    weather_summary: str | None = None


_DEFAULT_MANUAL = 3000
_DEFAULT_AUTO_STUB = 3000


@dataclass
class WaterHeaterReservedPolicy:
    """Persisted state — mode + manual_value. Auto cache lives in service."""

    mode: ReservedMode = ReservedMode.AUTO
    manual_value: int = _DEFAULT_MANUAL

    def compute_current_value(
        self,
        now: datetime,  # noqa: ARG002 — reserved for future logic
        input: WaterHeaterReservedInput,  # noqa: ARG002, A002 — reserved
    ) -> int:
        """Pure decision — return the effective reserved-power value.

        MANUAL → `manual_value` (user override via NumberEntity).
        AUTO → computed from inputs (stub: constant 3000 W). TODO: replace
        with logic based on RCE prices + PV forecast + weather summary
        (parity z planowanym Etap 2G BatteryScheduleProposer).
        """
        if self.mode == ReservedMode.MANUAL:
            return self.manual_value
        return _DEFAULT_AUTO_STUB

    def set_mode(self, mode: ReservedMode) -> bool:
        """Idempotent — returns True if changed."""
        if self.mode == mode:
            return False
        self.mode = mode
        return True

    def set_manual_value(self, value: int) -> bool:
        """Idempotent — returns True if changed."""
        if self.manual_value == value:
            return False
        self.manual_value = value
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "manual_value": self.manual_value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WaterHeaterReservedPolicy:
        mode_raw = data.get("mode", ReservedMode.AUTO.value)
        try:
            mode = ReservedMode(mode_raw)
        except ValueError:
            mode = ReservedMode.AUTO
        return cls(
            mode=mode,
            manual_value=int(data.get("manual_value", _DEFAULT_MANUAL)),
        )
