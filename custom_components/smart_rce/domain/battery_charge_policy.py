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
from datetime import datetime, time
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .charge_slots import (
    DEFAULT_ABSOLUTE_CHEAP_PRICE,
    DEFAULT_BASE_WINDOW_SHIFT_MIN,
    DEFAULT_EXTEND_THRESHOLD,
    DEFAULT_INITIAL_HOURS,
    ChargeWindowParams,
)

if TYPE_CHECKING:
    from .battery_schedule import BatteryOperation


class OverrideMode(StrEnum):
    """User-controlled override of charge enablement."""

    OFF = "OFF"
    ALLOWED = "ALLOWED"
    DISALLOWED = "DISALLOWED"


CHARGE_CURRENT_MAX_AMPS: float = 18.5
CHARGE_CURRENT_OFF_AMPS: float = 0.0

# Morning block-window start — legacy "_ Inverter DISABLE Battery Charge on 07:00"
# (automation 1717098789420) fires at 06:00:20 and turns off the legacy charge
# toggle. Time-gate uses 06:00 sharp.
BLOCK_WINDOW_START: time = time(6, 0)


@dataclass
class BatteryChargePolicy:
    """Aggregate root for charge enablement decision + Modbus state cache.

    Persisted fields = real state (user input + Modbus readback cache).
    Decision logic is pure functions taking per-tick inputs (`now`,
    `schedule_op`) — no shadow fields that mirror inputs.
    """

    charge_allowed_override: OverrideMode = OverrideMode.OFF
    _modbus_current_value: float | None = None
    _last_modbus_read_at: datetime | None = None
    # Etap B' time-gate: charging is BLOCKED in `[BLOCK_WINDOW_START=06:00,
    # start_charge_hour_override)`. Outside that window charging is ALLOWED.
    # For start < 06:00 the block window wraps midnight:
    # `[06:00, 24:00) U [00:00, start)`. When None, time-gate is disabled
    # (defers entirely to schedule). Fed from `state.start_charge_hour_override`
    # (legacy input_datetime) for now; future commit migrates ownership to
    # smart_rce time entity + drops state_mapper bridge.
    start_charge_hour_override: time | None = None
    # User-tunable inputs to the charge-window selection algorithm (consumed by
    # `ChargeSlots.compute` cross-aggregate via Ems on recompute). Kept here
    # because this policy already owns a crash-safe Store and groups the other
    # charge-window knobs. Exposed as a VO via `charge_window_params()`.
    initial_charge_hours: int = DEFAULT_INITIAL_HOURS
    charge_extend_threshold: float = DEFAULT_EXTEND_THRESHOLD
    charge_absolute_cheap_price: float = DEFAULT_ABSOLUTE_CHEAP_PRICE
    charge_base_window_shift_minutes: int = DEFAULT_BASE_WINDOW_SHIFT_MIN

    def charge_window_params(self) -> ChargeWindowParams:
        return ChargeWindowParams(
            initial_hours=self.initial_charge_hours,
            extend_threshold=self.charge_extend_threshold,
            absolute_cheap_price=self.charge_absolute_cheap_price,
            base_window_shift_minutes=self.charge_base_window_shift_minutes,
        )

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
        3. Schedule engagement — `schedule_op.needs_charge_toggle` → True
           (a CHARGE slot beats any time-gate block — slots are explicit
           user/proposer intent)
        4. Time-gate — within block window `[06:00, start_charge_hour)` → False
        5. Time-gate — outside block window (gate defined) → True
        6. Default — off (no gate, no schedule engagement)

        Time-gate semantic mirrors legacy YAML automations
        (`_ Inverter DISABLE Battery Charge on 07:00` at 06:00:20 + `_ Inverter
        ENABLE Battery Charge MORNING` at start_charge_hour_override). Toggle
        is OFF in `[06:00, start_charge_hour)` and ON elsewhere. For
        start < 06:00, the block window wraps midnight:
        `[06:00, 24:00) U [00:00, start)`. When start is None, gate is
        disabled — schedule decides, default off.
        """
        if self.charge_allowed_override == OverrideMode.DISALLOWED:
            return False
        if self.charge_allowed_override == OverrideMode.ALLOWED:
            return True
        # Schedule CHARGE slot wins — overrides time-gate block.
        if schedule_op.needs_charge_toggle:
            return True
        # Schedule says no — defer to time-gate.
        start = self.start_charge_hour_override
        if start is None:
            return False
        now_t = now.time()
        # Block window is `[BLOCK_WINDOW_START, start)`. If start < 06:00 the
        # window wraps midnight and we OR the two segments.
        if start >= BLOCK_WINDOW_START:
            in_block_window = BLOCK_WINDOW_START <= now_t < start
        else:
            in_block_window = now_t >= BLOCK_WINDOW_START or now_t < start
        return not in_block_window

    def target_modbus_value(
        self, now: datetime, schedule_op: BatteryOperation
    ) -> float:
        """Modbus value to write (18.5 A if allowed, 0.0 A otherwise)."""
        if self.charge_allowed(now, schedule_op):
            return CHARGE_CURRENT_MAX_AMPS
        return CHARGE_CURRENT_OFF_AMPS

    def set_charge_allowed_override(self, mode: OverrideMode) -> bool:
        """Mutate the charge-allowed override mode. Returns True if value changed."""
        if self.charge_allowed_override == mode:
            return False
        self.charge_allowed_override = mode
        return True

    def set_start_charge_hour_override(self, value: time | None) -> bool:
        """Mutate morning charge window start. Returns True if value changed."""
        if self.start_charge_hour_override == value:
            return False
        self.start_charge_hour_override = value
        return True

    def set_initial_charge_hours(self, value: int) -> bool:
        """Mutate the base charge-window length. Returns True if changed."""
        if self.initial_charge_hours == value:
            return False
        self.initial_charge_hours = value
        return True

    def set_charge_extend_threshold(self, value: float) -> bool:
        """Mutate the earlier-window extend threshold. Returns True if changed."""
        if self.charge_extend_threshold == value:
            return False
        self.charge_extend_threshold = value
        return True

    def set_charge_absolute_cheap_price(self, value: float) -> bool:
        """Mutate the absolute-cheap extend price. Returns True if changed."""
        if self.charge_absolute_cheap_price == value:
            return False
        self.charge_absolute_cheap_price = value
        return True

    def set_charge_base_window_shift_minutes(self, value: int) -> bool:
        """Mutate the base-window start shift (minutes). Returns True if changed."""
        if self.charge_base_window_shift_minutes == value:
            return False
        self.charge_base_window_shift_minutes = value
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
            "charge_allowed_override": self.charge_allowed_override.value,
            "modbus_current_value": self._modbus_current_value,
            "last_modbus_read_at": (
                self._last_modbus_read_at.isoformat()
                if self._last_modbus_read_at is not None
                else None
            ),
            "start_charge_hour_override": (
                self.start_charge_hour_override.isoformat()
                if self.start_charge_hour_override is not None
                else None
            ),
            "initial_charge_hours": self.initial_charge_hours,
            "charge_extend_threshold": self.charge_extend_threshold,
            "charge_absolute_cheap_price": self.charge_absolute_cheap_price,
            "charge_base_window_shift_minutes": self.charge_base_window_shift_minutes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatteryChargePolicy:
        """Restore from persisted dict. Tolerant of missing / bad fields.

        Reads the new `charge_allowed_override` key (rename from previous
        `user_override_mode` — domain-clearer naming). Legacy persisted
        installs fall back to the old key during one migration cycle; on
        next save the new key is written.
        """
        mode_raw = (
            data.get("charge_allowed_override")
            or data.get("user_override_mode")
            or OverrideMode.OFF.value
        )
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

        start_charge_raw = data.get("start_charge_hour_override")
        start_charge: time | None
        if start_charge_raw is not None:
            try:
                start_charge = time.fromisoformat(start_charge_raw)
            except (TypeError, ValueError):
                start_charge = None
        else:
            start_charge = None

        # Migration: legacy `charge_hours_override` (int | None, where None=Auto)
        # maps to `initial_charge_hours` (None → default base 3).
        legacy_hours = data.get("charge_hours_override")
        initial_hours = _coerce_int(
            data.get("initial_charge_hours", legacy_hours), DEFAULT_INITIAL_HOURS
        )
        extend_threshold = _coerce_float(
            data.get("charge_extend_threshold"), DEFAULT_EXTEND_THRESHOLD
        )
        absolute_cheap_price = _coerce_float(
            data.get("charge_absolute_cheap_price"), DEFAULT_ABSOLUTE_CHEAP_PRICE
        )
        base_window_shift = _coerce_int(
            data.get("charge_base_window_shift_minutes"), DEFAULT_BASE_WINDOW_SHIFT_MIN
        )

        return cls(
            charge_allowed_override=mode,
            _modbus_current_value=modbus_value,
            _last_modbus_read_at=last_read_at,
            start_charge_hour_override=start_charge,
            initial_charge_hours=initial_hours,
            charge_extend_threshold=extend_threshold,
            charge_absolute_cheap_price=absolute_cheap_price,
            charge_base_window_shift_minutes=base_window_shift,
        )


def _coerce_int(raw: Any, default: int) -> int:
    """Best-effort int from persisted value; `default` on None/garbage."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _coerce_float(raw: Any, default: float) -> float:
    """Best-effort float from persisted value; `default` on None/garbage."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default
