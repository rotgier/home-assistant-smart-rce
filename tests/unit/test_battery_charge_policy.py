"""Unit tests for BatteryChargePolicy domain entity."""

from datetime import datetime, timezone

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
        return BatteryOperation.from_entry(entry)

    def _discharge_evening_op(self) -> BatteryOperation:
        from custom_components.smart_rce.domain.battery_schedule import (
            BatteryScheduleEntry,
        )

        entry = BatteryScheduleEntry.default_for(SlotKind.DISCHARGE_EVENING)
        return BatteryOperation.from_entry(entry)

    def test_default_off_no_engagement(self):
        policy = BatteryChargePolicy()  # OverrideMode.OFF, no schedule
        assert policy.charge_allowed(_at(12), _idle_op()) is False

    def test_off_passthrough_to_schedule_charge(self):
        """OFF (passthrough) — CHARGE slot engagement enables charge."""
        policy = BatteryChargePolicy(user_override_mode=OverrideMode.OFF)
        assert policy.charge_allowed(_at(3), self._charge_morning_op()) is True

    def test_off_passthrough_discharge_does_not_enable(self):
        """OFF (passthrough) — DISCHARGE slot does NOT enable charge."""
        policy = BatteryChargePolicy(user_override_mode=OverrideMode.OFF)
        assert policy.charge_allowed(_at(20), self._discharge_evening_op()) is False

    def test_allowed_overrides_idle(self):
        policy = BatteryChargePolicy(user_override_mode=OverrideMode.ALLOWED)
        assert policy.charge_allowed(_at(12), _idle_op()) is True

    def test_allowed_overrides_discharge_engagement(self):
        """User ALLOWED forces charge ON even during a DISCHARGE slot."""
        policy = BatteryChargePolicy(user_override_mode=OverrideMode.ALLOWED)
        assert policy.charge_allowed(_at(20), self._discharge_evening_op()) is True

    def test_disallowed_blocks_schedule_charge(self):
        """User DISALLOWED forces charge OFF even during a CHARGE slot."""
        policy = BatteryChargePolicy(user_override_mode=OverrideMode.DISALLOWED)
        assert policy.charge_allowed(_at(3), self._charge_morning_op()) is False

    def test_disallowed_blocks_idle(self):
        policy = BatteryChargePolicy(user_override_mode=OverrideMode.DISALLOWED)
        assert policy.charge_allowed(_at(12), _idle_op()) is False


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
        policy = BatteryChargePolicy(user_override_mode=OverrideMode.ALLOWED)
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
        policy = BatteryChargePolicy(user_override_mode=OverrideMode.OFF)
        assert policy.set_user_override_mode(OverrideMode.ALLOWED) is True
        assert policy.user_override_mode == OverrideMode.ALLOWED

    def test_same_returns_false(self):
        policy = BatteryChargePolicy(user_override_mode=OverrideMode.OFF)
        assert policy.set_user_override_mode(OverrideMode.OFF) is False
        assert policy.user_override_mode == OverrideMode.OFF


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
        assert restored.user_override_mode == OverrideMode.OFF
        assert restored.modbus_current_value is None
        assert restored.last_modbus_read_at is None

    def test_full_round_trip(self):
        original = BatteryChargePolicy(
            user_override_mode=OverrideMode.ALLOWED,
            _modbus_current_value=18.5,
            _last_modbus_read_at=_at(8, 15),
        )
        restored = BatteryChargePolicy.from_dict(original.to_dict())
        assert restored.user_override_mode == OverrideMode.ALLOWED
        assert restored.modbus_current_value == 18.5
        assert restored.last_modbus_read_at == _at(8, 15)

    def test_invalid_override_mode_defaults_to_off(self):
        restored = BatteryChargePolicy.from_dict({"user_override_mode": "GARBAGE"})
        assert restored.user_override_mode == OverrideMode.OFF

    def test_invalid_modbus_value_becomes_none(self):
        restored = BatteryChargePolicy.from_dict(
            {"user_override_mode": "OFF", "modbus_current_value": "not_a_number"}
        )
        assert restored.modbus_current_value is None

    def test_invalid_timestamp_becomes_none(self):
        restored = BatteryChargePolicy.from_dict(
            {
                "user_override_mode": "OFF",
                "last_modbus_read_at": "not_iso_datetime",
            }
        )
        assert restored.last_modbus_read_at is None

    def test_missing_fields_use_defaults(self):
        restored = BatteryChargePolicy.from_dict({})
        assert restored.user_override_mode == OverrideMode.OFF
        assert restored.modbus_current_value is None
        assert restored.last_modbus_read_at is None
