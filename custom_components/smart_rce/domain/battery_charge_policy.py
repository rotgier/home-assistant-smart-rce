"""BatteryChargePolicy — domain aggregate for battery charge enablement.

Owns:
- User-controlled override mode (OFF / ALLOWED / DISALLOWED — select entity)
- Cached Modbus readback of the `battery_charge_current` register
  (smart_rce manages this because Goodwe HA integration doesn't expose an
  entity for it — only `goodwe.set_parameter` / `goodwe.get_parameter`
  services on Modbus register 45353, Kind.BAT).

Decision computation (pure functions — no shadow state):
- `charge_allowed(now, schedule_op) → bool` — combines override mode and
  schedule engagement. Etap B' will add time-gate logic (default off after
  06:00 unless `start_charge_hour_override` reached).
- `target_modbus_value(now, schedule_op) → float` — 18.5 A when allowed,
  0.0 A when not. Mirrors the legacy adapter automations
  `Inverter TOGGLED Battery Charge Current to MAX/ZERO`.

NOT persisted: `_last_computed_allowed`-style shadow field. Deltas detected
by `BatteryChargeCurrentActuator` via state-diff (target vs cached Modbus
readback) — single source of truth.

Replaces `input_boolean.battery_charge_max_current_toggle` (RestoreStateData
15-min cycle, lossy on crash). Persisted via own Store (~1s crash safety per
ADR-018). Etap B migration introduces this policy with passthrough-only
`charge_allowed` semantics; Etap B' adds time-gates + `start_charge_hour`
field migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .battery_schedule import BatteryOperation


class OverrideMode(StrEnum):
    """User-controlled override of charge enablement."""

    OFF = "OFF"
    ALLOWED = "ALLOWED"
    DISALLOWED = "DISALLOWED"


CHARGE_CURRENT_MAX_AMPS: float = 18.5
CHARGE_CURRENT_OFF_AMPS: float = 0.0


@dataclass
class BatteryChargePolicy:
    """Aggregate root for charge enablement decision + Modbus state cache.

    Persisted fields = real state (user input + Modbus readback cache).
    Decision logic is pure functions taking per-tick inputs (`now`,
    `schedule_op`) — no shadow fields that mirror inputs.
    """

    user_override_mode: OverrideMode = OverrideMode.OFF
    _modbus_current_value: float | None = None
    _last_modbus_read_at: datetime | None = None
    # Etap B' will add: start_charge_hour_override: time | None = None

    @property
    def modbus_current_value(self) -> float | None:
        return self._modbus_current_value

    @property
    def last_modbus_read_at(self) -> datetime | None:
        return self._last_modbus_read_at

    def charge_allowed(self, now: datetime, schedule_op: BatteryOperation) -> bool:
        """Pure decision — no mutation.

        Precedence (highest first):
        1. User DISALLOWED — block everything
        2. User ALLOWED — force on
        3. Schedule engagement — `schedule_op.needs_charge_toggle`
        4. (Etap B') Time-gate — default off after 06:00 unless
           `start_charge_hour_override` reached
        5. Default — off
        """
        if self.user_override_mode == OverrideMode.DISALLOWED:
            return False
        if self.user_override_mode == OverrideMode.ALLOWED:
            return True
        # OFF (passthrough) — defer to schedule. Etap B' will add time-gates here.
        return schedule_op.needs_charge_toggle

    def target_modbus_value(
        self, now: datetime, schedule_op: BatteryOperation
    ) -> float:
        """Modbus value to write (18.5 A if allowed, 0.0 A otherwise)."""
        if self.charge_allowed(now, schedule_op):
            return CHARGE_CURRENT_MAX_AMPS
        return CHARGE_CURRENT_OFF_AMPS

    def set_user_override_mode(self, mode: OverrideMode) -> bool:
        """Mutate user override. Returns True if value changed (caller persists)."""
        if self.user_override_mode == mode:
            return False
        self.user_override_mode = mode
        return True

    def record_modbus_read(self, value: float, at: datetime) -> bool:
        """Update Modbus state cache. Returns True if value changed.

        `_last_modbus_read_at` always updated (even when value unchanged) so
        callers can tell the cache is fresh. Equality check on value avoids
        persisting non-deltas to disk.
        """
        changed = self._modbus_current_value != value
        self._modbus_current_value = value
        self._last_modbus_read_at = at
        return changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_override_mode": self.user_override_mode.value,
            "modbus_current_value": self._modbus_current_value,
            "last_modbus_read_at": (
                self._last_modbus_read_at.isoformat()
                if self._last_modbus_read_at is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatteryChargePolicy:
        """Restore from persisted dict. Tolerant of missing / bad fields."""
        mode_raw = data.get("user_override_mode", OverrideMode.OFF.value)
        try:
            mode = OverrideMode(mode_raw)
        except ValueError:
            mode = OverrideMode.OFF

        last_read_raw = data.get("last_modbus_read_at")
        last_read_at: datetime | None
        if last_read_raw is not None:
            try:
                last_read_at = datetime.fromisoformat(last_read_raw)
            except (TypeError, ValueError):
                last_read_at = None
        else:
            last_read_at = None

        modbus_value_raw = data.get("modbus_current_value")
        modbus_value: float | None
        if modbus_value_raw is not None:
            try:
                modbus_value = float(modbus_value_raw)
            except (TypeError, ValueError):
                modbus_value = None
        else:
            modbus_value = None

        return cls(
            user_override_mode=mode,
            _modbus_current_value=modbus_value,
            _last_modbus_read_at=last_read_at,
        )
