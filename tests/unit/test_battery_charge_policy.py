"""Unit tests for BatteryChargePolicy domain entity."""

from datetime import datetime, time, timezone

from custom_components.smart_rce.domain.battery_charge_policy import (
    CHARGE_CURRENT_MAX_AMPS,
    CHARGE_CURRENT_OFF_AMPS,
    BatteryChargePolicy,
    OverrideMode,
)
from custom_components.smart_rce.domain.battery_schedule import (
    BatteryOperation,
    SlotKind,
)
import pytest

TZ = timezone.utc


def _at(h: int, m: int = 0) -> datetime:
    """Return datetime 2026-05-22 at HH:MM UTC (test helper)."""
    return datetime(2026, 5, 22, h, m, tzinfo=TZ)


def _idle_op() -> BatteryOperation:
    return BatteryOperation.idle()


# ─────────────────────────────────────────────────────────────────────────────
# OverrideMode
# ─────────────────────────────────────────────────────────────────────────────


class TestOverrideMode:
    def test_values(self):
        assert OverrideMode.OFF.value == "OFF"
        assert OverrideMode.ALLOWED.value == "ALLOWED"
        assert OverrideMode.DISALLOWED.value == "DISALLOWED"

    def test_from_str(self):
        assert OverrideMode("OFF") == OverrideMode.OFF
        assert OverrideMode("ALLOWED") == OverrideMode.ALLOWED

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            OverrideMode("INVALID")


# ─────────────────────────────────────────────────────────────────────────────
# charge_allowed
# ─────────────────────────────────────────────────────────────────────────────


class TestChargeAllowed:
    def _charge_morning_op(self) -> BatteryOperation:
        from custom_components.smart_rce.domain.battery_schedule import (
            BatteryScheduleEntry,
        )

        entry = BatteryScheduleEntry.default_for(SlotKind.CHARGE_MORNING)
        return entry.to_battery_operation()

    def _discharge_evening_op(self) -> BatteryOperation:
        from custom_components.smart_rce.domain.battery_schedule import (
            BatteryScheduleEntry,
        )

        entry = BatteryScheduleEntry.default_for(SlotKind.DISCHARGE_EVENING)
        return entry.to_battery_operation()

    def test_default_off_no_engagement(self):
        policy = BatteryChargePolicy()  # OverrideMode.OFF, no schedule
        assert policy.charge_allowed(_at(12), _idle_op()) is False

    def test_off_passthrough_to_schedule_charge(self):
        """OFF (passthrough) — CHARGE slot engagement enables charge."""
        policy = BatteryChargePolicy(charge_allowed_override=OverrideMode.OFF)
        assert policy.charge_allowed(_at(3), self._charge_morning_op()) is True

    def test_off_passthrough_discharge_does_not_enable(self):
        """OFF (passthrough) — DISCHARGE slot does NOT enable charge."""
        policy = BatteryChargePolicy(charge_allowed_override=OverrideMode.OFF)
        assert policy.charge_allowed(_at(20), self._discharge_evening_op()) is False

    def test_allowed_overrides_idle(self):
        policy = BatteryChargePolicy(charge_allowed_override=OverrideMode.ALLOWED)
        assert policy.charge_allowed(_at(12), _idle_op()) is True

    def test_allowed_overrides_discharge_engagement(self):
        """User ALLOWED forces charge ON even during a DISCHARGE slot."""
        policy = BatteryChargePolicy(charge_allowed_override=OverrideMode.ALLOWED)
        assert policy.charge_allowed(_at(20), self._discharge_evening_op()) is True

    def test_disallowed_blocks_schedule_charge(self):
        """User DISALLOWED forces charge OFF even during a CHARGE slot."""
        policy = BatteryChargePolicy(charge_allowed_override=OverrideMode.DISALLOWED)
        assert policy.charge_allowed(_at(3), self._charge_morning_op()) is False

    def test_disallowed_blocks_idle(self):
        policy = BatteryChargePolicy(charge_allowed_override=OverrideMode.DISALLOWED)
        assert policy.charge_allowed(_at(12), _idle_op()) is False


# ─────────────────────────────────────────────────────────────────────────────
# Time-gate (Etap B') — block window [06:00, start_charge_hour); ALLOWED outside
# ─────────────────────────────────────────────────────────────────────────────


class TestChargeAllowedTimeGate:
    def test_no_override_defers_to_schedule(self):
        """start_charge_hour_override=None → time-gate disabled, schedule decides."""
        policy = BatteryChargePolicy(start_charge_hour_override=None)
        assert policy.charge_allowed(_at(3, 30), _idle_op()) is False

    # ── start >= 06:00 (e.g., 11:00) — block window [06:00, start) — no wrap ──

    def test_start_after_06_block_window_blocks(self):
        """start=11:00, now=09:00 in [06:00, 11:00) → blocked."""
        policy = BatteryChargePolicy(start_charge_hour_override=time(11, 0))
        assert policy.charge_allowed(_at(9, 0), _idle_op()) is False

    def test_start_after_06_at_06_inclusive(self):
        """06:00 sharp → block window STARTS (inclusive)."""
        policy = BatteryChargePolicy(start_charge_hour_override=time(11, 0))
        assert policy.charge_allowed(_at(6, 0), _idle_op()) is False

    def test_start_after_06_at_start_exclusive(self):
        """11:00 sharp = start → block window CLOSES, allowed."""
        policy = BatteryChargePolicy(start_charge_hour_override=time(11, 0))
        assert policy.charge_allowed(_at(11, 0), _idle_op()) is True

    def test_start_after_06_before_block_window_allowed(self):
        """05:00 < 06:00 → before block window → ALLOWED."""
        policy = BatteryChargePolicy(start_charge_hour_override=time(11, 0))
        assert policy.charge_allowed(_at(5, 0), _idle_op()) is True

    def test_start_after_06_after_start_allowed(self):
        """14:00 > 11:00 → past start → ALLOWED."""
        policy = BatteryChargePolicy(start_charge_hour_override=time(11, 0))
        assert policy.charge_allowed(_at(14, 0), _idle_op()) is True

    def test_start_after_06_late_evening_allowed(self):
        """23:00 → far past start, still ALLOWED."""
        policy = BatteryChargePolicy(start_charge_hour_override=time(11, 0))
        assert policy.charge_allowed(_at(23, 0), _idle_op()) is True

    # ── start < 06:00 (e.g., 02:00) — block window wraps midnight ──
    # Block: [06:00, 24:00) U [00:00, 02:00). Allowed: [02:00, 06:00).

    def test_start_before_06_inside_charge_window_allowed(self):
        """start=02:00, now=04:00 in [02:00, 06:00) → ALLOWED."""
        policy = BatteryChargePolicy(start_charge_hour_override=time(2, 0))
        assert policy.charge_allowed(_at(4, 0), _idle_op()) is True

    def test_start_before_06_after_06_blocked(self):
        """07:00 → past 06:00 disable → BLOCKED."""
        policy = BatteryChargePolicy(start_charge_hour_override=time(2, 0))
        assert policy.charge_allowed(_at(7, 0), _idle_op()) is False

    def test_start_before_06_late_evening_blocked(self):
        """23:00 → still in OFF window (block wraps midnight) → BLOCKED."""
        policy = BatteryChargePolicy(start_charge_hour_override=time(2, 0))
        assert policy.charge_allowed(_at(23, 0), _idle_op()) is False

    def test_start_before_06_pre_dawn_blocked(self):
        """01:00 < 02:00 start → still in wrapped block window → BLOCKED."""
        policy = BatteryChargePolicy(start_charge_hour_override=time(2, 0))
        assert policy.charge_allowed(_at(1, 0), _idle_op()) is False

    def test_start_before_06_at_start_allowed(self):
        """02:00 sharp = start → ALLOWED (block window closes)."""
        policy = BatteryChargePolicy(start_charge_hour_override=time(2, 0))
        assert policy.charge_allowed(_at(2, 0), _idle_op()) is True

    # ── User override beats time-gate ──

    def test_disallowed_overrides_time_gate(self):
        """DISALLOWED beats time-gate even in the ALLOWED period."""
        policy = BatteryChargePolicy(
            charge_allowed_override=OverrideMode.DISALLOWED,
            start_charge_hour_override=time(2, 0),
        )
        assert policy.charge_allowed(_at(4, 0), _idle_op()) is False

    def test_allowed_overrides_time_gate_block(self):
        """ALLOWED forces on even in the time-gate BLOCK window."""
        policy = BatteryChargePolicy(
            charge_allowed_override=OverrideMode.ALLOWED,
            start_charge_hour_override=time(11, 0),
        )
        assert policy.charge_allowed(_at(9, 0), _idle_op()) is True

    def test_schedule_charge_slot_beats_time_gate_block(self):
        """Active CHARGE slot wins over time-gate BLOCK window.

        Slots are explicit user/proposer intent — time-gate is the fallback.
        If schedule says 'charge', we charge even at 09:00 with start=11:00.
        """
        from custom_components.smart_rce.domain.battery_schedule import (
            BatteryScheduleEntry,
        )

        charge_op = BatteryScheduleEntry.default_for(
            SlotKind.CHARGE_MORNING
        ).to_battery_operation()
        policy = BatteryChargePolicy(start_charge_hour_override=time(11, 0))
        # 09:00 is in block window [06:00, 11:00) — but schedule says charge.
        assert policy.charge_allowed(_at(9, 0), charge_op) is True


# ─────────────────────────────────────────────────────────────────────────────
# target_modbus_value
# ─────────────────────────────────────────────────────────────────────────────


class TestTargetModbusValue:
    def test_off_default(self):
        policy = BatteryChargePolicy()
        assert (
            policy.target_modbus_value(_at(12), _idle_op()) == CHARGE_CURRENT_OFF_AMPS
        )

    def test_allowed_max(self):
        policy = BatteryChargePolicy(charge_allowed_override=OverrideMode.ALLOWED)
        assert (
            policy.target_modbus_value(_at(12), _idle_op()) == CHARGE_CURRENT_MAX_AMPS
        )

    def test_constants(self):
        assert CHARGE_CURRENT_MAX_AMPS == 18.5
        assert CHARGE_CURRENT_OFF_AMPS == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Mutators
# ─────────────────────────────────────────────────────────────────────────────


class TestSetUserOverrideMode:
    def test_changes_returns_true(self):
        policy = BatteryChargePolicy(charge_allowed_override=OverrideMode.OFF)
        assert policy.set_charge_allowed_override(OverrideMode.ALLOWED) is True
        assert policy.charge_allowed_override == OverrideMode.ALLOWED

    def test_same_returns_false(self):
        policy = BatteryChargePolicy(charge_allowed_override=OverrideMode.OFF)
        assert policy.set_charge_allowed_override(OverrideMode.OFF) is False
        assert policy.charge_allowed_override == OverrideMode.OFF


class TestRecordModbusRead:
    def test_first_read_returns_true(self):
        policy = BatteryChargePolicy()
        assert policy.record_modbus_read(18.5, _at(12)) is True
        assert policy.modbus_current_value == 18.5
        assert policy.last_modbus_read_at == _at(12)

    def test_same_value_returns_false_but_updates_timestamp(self):
        policy = BatteryChargePolicy(
            _modbus_current_value=18.5,
            _last_modbus_read_at=_at(11),
        )
        assert policy.record_modbus_read(18.5, _at(12)) is False
        assert policy.modbus_current_value == 18.5
        # Timestamp still updated (we know the cache is fresh).
        assert policy.last_modbus_read_at == _at(12)

    def test_different_value_returns_true(self):
        policy = BatteryChargePolicy(
            _modbus_current_value=18.5, _last_modbus_read_at=_at(11)
        )
        assert policy.record_modbus_read(0.0, _at(12)) is True
        assert policy.modbus_current_value == 0.0
        assert policy.last_modbus_read_at == _at(12)


# ─────────────────────────────────────────────────────────────────────────────
# Persistence round-trip
# ─────────────────────────────────────────────────────────────────────────────


class TestPersistence:
    def test_default_round_trip(self):
        policy = BatteryChargePolicy()
        restored = BatteryChargePolicy.from_dict(policy.to_dict())
        assert restored.charge_allowed_override == OverrideMode.OFF
        assert restored.modbus_current_value is None
        assert restored.last_modbus_read_at is None

    def test_full_round_trip(self):
        original = BatteryChargePolicy(
            charge_allowed_override=OverrideMode.ALLOWED,
            _modbus_current_value=18.5,
            _last_modbus_read_at=_at(8, 15),
        )
        restored = BatteryChargePolicy.from_dict(original.to_dict())
        assert restored.charge_allowed_override == OverrideMode.ALLOWED
        assert restored.modbus_current_value == 18.5
        assert restored.last_modbus_read_at == _at(8, 15)

    def test_invalid_override_mode_defaults_to_off(self):
        restored = BatteryChargePolicy.from_dict({"charge_allowed_override": "GARBAGE"})
        assert restored.charge_allowed_override == OverrideMode.OFF

    def test_invalid_modbus_value_becomes_none(self):
        restored = BatteryChargePolicy.from_dict(
            {"charge_allowed_override": "OFF", "modbus_current_value": "not_a_number"}
        )
        assert restored.modbus_current_value is None

    def test_invalid_timestamp_becomes_none(self):
        restored = BatteryChargePolicy.from_dict(
            {
                "charge_allowed_override": "OFF",
                "last_modbus_read_at": "not_iso_datetime",
            }
        )
        assert restored.last_modbus_read_at is None

    def test_missing_fields_use_defaults(self):
        restored = BatteryChargePolicy.from_dict({})
        assert restored.charge_allowed_override == OverrideMode.OFF
        assert restored.modbus_current_value is None
        assert restored.last_modbus_read_at is None
        assert restored.start_charge_hour_override is None
        assert restored.charge_hours_override is None

    def test_charge_hours_override_persisted(self):
        original = BatteryChargePolicy(charge_hours_override=2)
        restored = BatteryChargePolicy.from_dict(original.to_dict())
        assert restored.charge_hours_override == 2

    def test_invalid_charge_hours_becomes_none(self):
        restored = BatteryChargePolicy.from_dict(
            {"charge_allowed_override": "OFF", "charge_hours_override": "not_int"}
        )
        assert restored.charge_hours_override is None

    def test_start_charge_hour_override_persisted(self):
        original = BatteryChargePolicy(start_charge_hour_override=time(2, 30))
        restored = BatteryChargePolicy.from_dict(original.to_dict())
        assert restored.start_charge_hour_override == time(2, 30)

    def test_invalid_start_charge_hour_becomes_none(self):
        restored = BatteryChargePolicy.from_dict(
            {"charge_allowed_override": "OFF", "start_charge_hour_override": "not_iso"}
        )
        assert restored.start_charge_hour_override is None


class TestSetStartChargeHourOverride:
    def test_changes_returns_true(self):
        policy = BatteryChargePolicy()
        assert policy.set_start_charge_hour_override(time(2, 30)) is True
        assert policy.start_charge_hour_override == time(2, 30)

    def test_same_returns_false(self):
        policy = BatteryChargePolicy(start_charge_hour_override=time(2, 30))
        assert policy.set_start_charge_hour_override(time(2, 30)) is False
        assert policy.start_charge_hour_override == time(2, 30)

    def test_set_to_none(self):
        policy = BatteryChargePolicy(start_charge_hour_override=time(2, 30))
        assert policy.set_start_charge_hour_override(None) is True
        assert policy.start_charge_hour_override is None


class TestSetChargeHoursOverride:
    def test_changes_returns_true(self):
        policy = BatteryChargePolicy()
        assert policy.set_charge_hours_override(2) is True
        assert policy.charge_hours_override == 2

    def test_same_returns_false(self):
        policy = BatteryChargePolicy(charge_hours_override=2)
        assert policy.set_charge_hours_override(2) is False

    def test_set_back_to_auto(self):
        policy = BatteryChargePolicy(charge_hours_override=2)
        assert policy.set_charge_hours_override(None) is True
        assert policy.charge_hours_override is None
