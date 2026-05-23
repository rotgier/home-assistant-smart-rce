"""WaterHeaterReservedPolicy — persisted state for reserved-power value (W).

Aggregate root for water heater reservation power (`reserved` in
`WaterHeaterManager._balanced_target` for `battery_charge_limit > 7`).
Two modes:
- AUTO: `compute_auto(now, input)` returns value (stub returns 3000 W;
  TODO: dynamic logic based on RCE prices + PV forecast + weather)
- MANUAL: returns `manual_value` set by user via UI (NumberEntity)

Persisted state is minimal — only `mode` + `manual_value`. The auto cache
(`_last_auto_value`) lives in `WaterHeaterReservedService` (in-memory,
recomputed every tick).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 — used in compute_auto signature
from enum import StrEnum
from typing import Any


class ReservedMode(StrEnum):
    """Source of `reserved` value.

    AUTO — service uses `compute_auto(now, input)` result.
    MANUAL — service returns `manual_value` (user-set via NumberEntity).
    """

    AUTO = "AUTO"
    MANUAL = "MANUAL"


@dataclass(frozen=True)
class WaterHeaterReservedInput:
    """Inputs for compute_auto. All fields optional — None = unavailable.

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

    def compute_auto(
        self,
        now: datetime,  # noqa: ARG002 — reserved for future logic
        input: WaterHeaterReservedInput,  # noqa: ARG002, A002 — reserved
    ) -> int:
        """Pure decision based on inputs.

        Stub: returns constant 3000 W. TODO: replace with logic based on
        RCE prices + PV forecast + weather summary (parity z planowanym
        Etap 2G BatteryScheduleProposer).
        """
        return _DEFAULT_AUTO_STUB

    def current_value(self, auto_value: int) -> int:
        """Return value to use NOW given precomputed auto.

        MANUAL → manual_value (user override)
        AUTO → auto_value (passed by service — fresh per-tick cache)
        """
        if self.mode == ReservedMode.MANUAL:
            return self.manual_value
        return auto_value

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
