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
from datetime import date, datetime, time
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .charge_slots import (
    DEFAULT_ABSOLUTE_CHEAP_PRICE,
    DEFAULT_BASE_WINDOW_SHIFT_MIN,
    DEFAULT_EXTEND_THRESHOLD,
    DEFAULT_INITIAL_HOURS,
    ChargeWindowParams,
)
from .charge_start_shift import ChargeStartShift, to_minutes
from .pv_charge import PvChargeRun

if TYPE_CHECKING:
    from .battery_schedule import BatteryOperation


class OverrideMode(StrEnum):
    """User-controlled override of charge enablement."""

    OFF = "OFF"
    ALLOWED = "ALLOWED"
    DISALLOWED = "DISALLOWED"


class ChargeStartSource(StrEnum):
    """Where TODAY's charge start in force came from.

    Diagnostic view over `start_charge_manual_day` + `forecast_shift`, which
    on their own require knowing today's date to interpret: a manual mark
    dated yesterday means "automatic" and reads as a leftover otherwise.
    A hand-set start for tomorrow is reported separately (not a combination
    here), so this stays a single answer about today.
    """

    AUTO = "auto"
    FORECAST = "forecast"
    MANUAL = "manual"


CHARGE_CURRENT_MAX_AMPS: float = 18.5
CHARGE_CURRENT_OFF_AMPS: float = 0.0

# Morning block-window start — legacy "_ Inverter DISABLE Battery Charge on 07:00"
# (automation 1717098789420) fires at 06:00:20 and turns off the legacy charge
# toggle. Time-gate uses 06:00 sharp.
BLOCK_WINDOW_START: time = time(6, 0)


class BatteryChargePolicy:
    """Aggregate root for charge enablement decision + Modbus state cache.

    Persisted fields = real state (user input + Modbus readback cache).
    Decision logic is pure functions taking per-tick inputs (`now`,
    `schedule_op`) — no shadow fields that mirror inputs.

    A plain class rather than a dataclass: an aggregate is compared by identity,
    and the constructor takes exactly what `from_dict` restores.
    """

    def __init__(
        self,
        *,
        charge_allowed_override: OverrideMode = OverrideMode.OFF,
        modbus_current_value: float | None = None,
        last_modbus_read_at: datetime | None = None,
        start_charge_hour_override: time | None = None,
        start_charge_manual_day: date | None = None,
        tomorrow_plan: ChargeStartPlan | None = None,
        forecast_shift: ChargeStartShift | None = None,
        pv_charge_run: PvChargeRun | None = None,
        initial_charge_hours: int = DEFAULT_INITIAL_HOURS,
        charge_extend_threshold: float = DEFAULT_EXTEND_THRESHOLD,
        charge_absolute_cheap_price: float = DEFAULT_ABSOLUTE_CHEAP_PRICE,
        charge_base_window_shift_minutes: int = DEFAULT_BASE_WINDOW_SHIFT_MIN,
    ) -> None:
        self.charge_allowed_override = charge_allowed_override
        self._modbus_current_value = modbus_current_value
        self._last_modbus_read_at = last_modbus_read_at
        # Etap B' time-gate: charging is BLOCKED in `[BLOCK_WINDOW_START=06:00,
        # start_charge_hour_override)`. Outside that window charging is ALLOWED.
        # For start < 06:00 the block window wraps midnight:
        # `[06:00, 24:00) U [00:00, start)`. When None, time-gate is disabled
        # (defers entirely to schedule).
        self.start_charge_hour_override = start_charge_hour_override
        # Provenance of `start_charge_hour_override`: the day for which the value
        # was set BY HAND. Auto-sync refuses to touch the override while this
        # equals today, so a manual value survives price refreshes AND restarts
        # (the flag is persisted; `ChargeSlots` is not). Storing a date rather
        # than a bool makes the manual mark expire on its own at midnight — no
        # clearing event to miss when HA is down over the rollover.
        self.start_charge_manual_day = start_charge_manual_day
        # Manual plan for TOMORROW's charge start, pinned to the day it applies
        # to. Unlike the today override this is NOT the effective value consumed
        # by the time-gate — it only exists while the user has overridden the
        # computed window, and is promoted into `start_charge_hour_override` on
        # the day it names. Absent (None) means "show the computed window".
        self.tomorrow_plan = tomorrow_plan
        # Today's charge start moved by the PV forecast (iteration 3): opens the
        # battery to PV earlier when the cheap-hours window alone won't fill it.
        # Pinned to its day like `tomorrow_plan`; a hand-set start for today
        # still wins — the shift keeps being evaluated, only not applied.
        self.forecast_shift = forecast_shift
        # Today's PV charge as it happened (opened, 90%, full) — read model for
        # the price chart, persisted so a restart mid-day keeps it.
        self.pv_charge_run = pv_charge_run
        # User-tunable inputs to the charge-window selection algorithm (consumed
        # by `ChargeSlots.compute` cross-aggregate via Ems on recompute). Kept
        # here because this policy already owns a crash-safe Store and groups the
        # other charge-window knobs. Exposed as a VO via `charge_window_params()`.
        self.initial_charge_hours = initial_charge_hours
        self.charge_extend_threshold = charge_extend_threshold
        self.charge_absolute_cheap_price = charge_absolute_cheap_price
        self.charge_base_window_shift_minutes = charge_base_window_shift_minutes

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
        return self._time_gate_open(now)

    def _time_gate_open(self, now: datetime) -> bool:
        """Tell whether the start-hour gate alone is open (steps 4-6 above)."""
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

    def start_charge_source(self, today: date) -> ChargeStartSource:
        """Classify today's start: from prices, moved by the forecast, or by hand.

        Pure — `today` is injected, mirroring `charge_allowed(now, ...)`. A
        manual mark or a shift for any other day is spent and counts as auto.
        """
        if self.start_charge_manual_day == today:
            return ChargeStartSource.MANUAL
        shift = self.forecast_shift
        if shift is not None and shift.day == today and shift.is_shifted:
            return ChargeStartSource.FORECAST
        return ChargeStartSource.AUTO

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
        """Mutate morning charge window start. Returns True if value changed.

        Provenance-neutral — used by the automatic sync and by the
        param-change force-sync. To mark the value as hand-set use
        `set_start_charge_hour_manual`.
        """
        if self.start_charge_hour_override == value:
            return False
        self.start_charge_hour_override = value
        return True

    def set_start_charge_hour_manual(self, value: time, day: date) -> bool:
        """Set today's start AND mark it as hand-set for `day`.

        While `start_charge_manual_day` equals the current date the automatic
        sync leaves the override alone, so the value survives price refreshes
        and restarts alike. Returns True if anything changed.
        """
        changed = self.set_start_charge_hour_override(value)
        changed |= self.start_charge_manual_day != day
        self.start_charge_manual_day = day
        return changed

    def clear_start_charge_manual(self) -> bool:
        """Drop the hand-set mark — the override goes back to following RCE."""
        if self.start_charge_manual_day is None:
            return False
        self.start_charge_manual_day = None
        return True

    def set_tomorrow_plan(self, value: time, day: date) -> bool:
        """Pin a manual charge start for `day` (tomorrow at the time of setting)."""
        plan = ChargeStartPlan(day=day, value=value)
        if self.tomorrow_plan == plan:
            return False
        self.tomorrow_plan = plan
        return True

    def clear_tomorrow_plan(self) -> bool:
        """Drop the manual plan — the tomorrow view falls back to the computed window."""
        if self.tomorrow_plan is None:
            return False
        self.tomorrow_plan = None
        return True

    def forecast_shift_for(self, today: date, s0: int) -> ChargeStartShift:
        """Today's shift, started from `s0` (minutes) when there is none yet."""
        if self.forecast_shift is None or self.forecast_shift.day != today:
            self.forecast_shift = ChargeStartShift(day=today, s0=s0)
        return self.forecast_shift

    def observe_pv_charge(self, now: datetime, soc: float, window_end: int) -> bool:
        """Record today's PV charge (start, 90%, full) — True when anything changed.

        The battery takes PV from the moment the time gate opens after 06:00 (or
        the user forces charging on); a charge slot from the grid does not count,
        nor the night, when the gate stands open by default.
        """
        today = now.date()
        changed = False
        if self.pv_charge_run is None or self.pv_charge_run.day != today:
            self.pv_charge_run = PvChargeRun(day=today)
            changed = True
        start = self.start_charge_hour_override
        return (
            self.pv_charge_run.observe(
                to_minutes(now.time()),
                soc,
                opened=self._opens_to_pv(now),
                start_in_force=to_minutes(start) if start is not None else None,
                window_end=window_end,
            )
            or changed
        )

    def _opens_to_pv(self, now: datetime) -> bool:
        if self.charge_allowed_override == OverrideMode.DISALLOWED:
            return False
        if now.time() < BLOCK_WINDOW_START:
            return False
        if self.charge_allowed_override == OverrideMode.ALLOWED:
            return True
        return self._time_gate_open(now)

    def drop_stale_forecast_shift(self, today: date) -> bool:
        """Forget a shift from an earlier day — it must not move today's start."""
        if self.forecast_shift is None or self.forecast_shift.day == today:
            return False
        self.forecast_shift = None
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
            "start_charge_manual_day": (
                self.start_charge_manual_day.isoformat()
                if self.start_charge_manual_day is not None
                else None
            ),
            "tomorrow_plan": (
                self.tomorrow_plan.to_dict() if self.tomorrow_plan is not None else None
            ),
            "forecast_shift": (
                self.forecast_shift.to_dict()
                if self.forecast_shift is not None
                else None
            ),
            "pv_charge_run": (
                self.pv_charge_run.to_dict() if self.pv_charge_run is not None else None
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

        start_charge = _coerce_time(data.get("start_charge_hour_override"))
        manual_day = _coerce_date(data.get("start_charge_manual_day"))
        tomorrow_plan = ChargeStartPlan.from_dict(data.get("tomorrow_plan"))

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
            modbus_current_value=modbus_value,
            last_modbus_read_at=last_read_at,
            start_charge_hour_override=start_charge,
            start_charge_manual_day=manual_day,
            tomorrow_plan=tomorrow_plan,
            forecast_shift=ChargeStartShift.from_dict(data.get("forecast_shift")),
            pv_charge_run=PvChargeRun.from_dict(data.get("pv_charge_run")),
            initial_charge_hours=initial_hours,
            charge_extend_threshold=extend_threshold,
            charge_absolute_cheap_price=absolute_cheap_price,
            charge_base_window_shift_minutes=base_window_shift,
        )


@dataclass(frozen=True)
class ChargeStartPlan:
    """A hand-set charge start pinned to the day it applies to.

    Value object — the day is part of the identity, which is what makes the
    plan safe across a restart or a missed midnight: whether it should take
    effect is answered by comparing dates, never by having caught an event.
    """

    day: date
    value: time

    def is_due(self, today: date) -> bool:
        """Tell whether the plan names today and should become the active override."""
        return self.day == today

    def is_stale(self, today: date) -> bool:
        """Tell whether the day has passed — such a plan can only be dropped."""
        return self.day < today

    def to_dict(self) -> dict[str, Any]:
        return {"day": self.day.isoformat(), "value": self.value.isoformat()}

    @classmethod
    def from_dict(cls, data: Any) -> ChargeStartPlan | None:
        """Restore from persisted dict; None on missing / unparsable input."""
        if not isinstance(data, dict):
            return None
        day = _coerce_date(data.get("day"))
        value = _coerce_time(data.get("value"))
        if day is None or value is None:
            return None
        return cls(day=day, value=value)


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


def _coerce_time(raw: Any) -> time | None:
    """Best-effort `time` from a persisted ISO string; None on missing/garbage."""
    try:
        return time.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _coerce_date(raw: Any) -> date | None:
    """Best-effort `date` from a persisted ISO string; None on missing/garbage."""
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
