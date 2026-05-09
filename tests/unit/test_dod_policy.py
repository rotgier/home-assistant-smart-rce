"""Unit tests for DodPolicy — phase dispatch + override + persistence."""

from datetime import datetime, time
from unittest.mock import MagicMock

from custom_components.smart_rce.domain.discharge_slots import (
    DischargeSlots,
    UpcomingPeak,
)
from custom_components.smart_rce.domain.dod_policy import DEFAULT_DOD, DodPolicy, Phase
from custom_components.smart_rce.domain.input_state import InputState
from custom_components.smart_rce.domain.rce import TIMEZONE


def _state(
    *,
    now,
    is_workday=True,
    is_workday_tomorrow=True,
    start_charge=time(10, 0),
    rce_should_hold_for_peak=False,
    ems_allow_discharge_override=False,
    dod_override=-1.0,
    rce_high_price_threshold_gross=350.0,
) -> InputState:
    return InputState(
        now=now,
        is_workday=is_workday,
        is_workday_tomorrow=is_workday_tomorrow,
        start_charge_hour_override=start_charge,
        rce_should_hold_for_peak=rce_should_hold_for_peak,
        ems_allow_discharge_override=ems_allow_discharge_override,
        dod_override=dod_override,
        rce_high_price_threshold_gross=rce_high_price_threshold_gross,
    )


def _discharge_slots(*, morning_price_netto: float | None = None) -> DischargeSlots:
    """DischargeSlots stub for night-preserve dispatch tests.

    morning_price_netto is what slot.price holds (netto). DodPolicy multiplies
    by GROSS_MULTIPLIER (1.23) for comparison vs threshold_gross. Default None
    → no slot, falls back to NIGHT_FREE if is_workday_tomorrow=False.
    """
    slots = DischargeSlots()
    if morning_price_netto is not None:
        slots.best_morning_discharge_slot = UpcomingPeak(
            price=morning_price_netto,
            datetime=datetime(2026, 5, 12, 7, 0, tzinfo=TIMEZONE),
        )
    return slots


def _at(h, m=0):
    return datetime(2026, 5, 11, h, m, tzinfo=TIMEZONE)  # 2026-05-11 = Monday (workday)


def _weekend_at(h, m=0):
    return datetime(2026, 5, 9, h, m, tzinfo=TIMEZONE)  # 2026-05-09 = Saturday


def _battery_mgr(*, block=False):
    m = MagicMock()
    m.should_block_battery_discharge = block
    return m


class TestPhaseDispatch:
    """_compute_phase returns correct phase for time + flags combinations."""

    def test_ems_allow_discharge_overrides_all(self):
        p = DodPolicy()
        s = _state(now=_at(10), ems_allow_discharge_override=True)
        assert p._compute_phase(s, _discharge_slots()) == Phase.EMS_ALLOW_DISCHARGE

    def test_workday_pre_charge(self):
        p = DodPolicy()
        s = _state(now=_at(8), is_workday=True, start_charge=time(10, 0))
        assert p._compute_phase(s, _discharge_slots()) == Phase.WORKDAY_PRE_CHARGE

    def test_workday_post_charge(self):
        p = DodPolicy()
        s = _state(now=_at(11), is_workday=True, start_charge=time(10, 0))
        assert p._compute_phase(s, _discharge_slots()) == Phase.WORKDAY_POST_CHARGE

    def test_weekend_morning(self):
        p = DodPolicy()
        s = _state(now=_weekend_at(9), is_workday=False)
        assert p._compute_phase(s, _discharge_slots()) == Phase.WEEKEND_MORNING

    def test_weekend_morning_at_7_sharp(self):
        p = DodPolicy()
        s = _state(now=_weekend_at(7), is_workday=False)
        assert p._compute_phase(s, _discharge_slots()) == Phase.WEEKEND_MORNING

    def test_afternoon_static_peak(self):
        p = DodPolicy()
        s = _state(now=_at(14), rce_should_hold_for_peak=True)
        assert p._compute_phase(s, _discharge_slots()) == Phase.AFTERNOON_STATIC

    def test_afternoon_dynamic_no_peak(self):
        p = DodPolicy()
        s = _state(now=_at(14), rce_should_hold_for_peak=False)
        assert p._compute_phase(s, _discharge_slots()) == Phase.AFTERNOON_DYNAMIC

    def test_afternoon_static_applies_on_weekend_too(self):
        p = DodPolicy()
        s = _state(now=_weekend_at(15), is_workday=False, rce_should_hold_for_peak=True)
        assert p._compute_phase(s, _discharge_slots()) == Phase.AFTERNOON_STATIC

    def test_evening(self):
        p = DodPolicy()
        s = _state(now=_at(20))
        assert p._compute_phase(s, _discharge_slots()) == Phase.EVENING

    def test_evening_applies_weekend_too(self):
        p = DodPolicy()
        s = _state(now=_weekend_at(20), is_workday=False)
        assert p._compute_phase(s, _discharge_slots()) == Phase.EVENING

    def test_night_preserve_workday_tomorrow(self):
        p = DodPolicy()
        s = _state(now=_at(23), is_workday_tomorrow=True)
        assert p._compute_phase(s, _discharge_slots()) == Phase.NIGHT_PRESERVE

    def test_night_preserve_expensive_morning(self):
        """Morning slot.price (netto) × 1.23 > threshold_gross → preserve."""
        p = DodPolicy()
        s = _state(
            now=_weekend_at(23),
            is_workday_tomorrow=False,
            rce_high_price_threshold_gross=350.0,
        )
        # 300 netto × 1.23 = 369 gross > 350 threshold → preserve
        slots = _discharge_slots(morning_price_netto=300.0)
        assert p._compute_phase(s, slots) == Phase.NIGHT_PRESERVE

    def test_night_free_cheap_morning(self):
        p = DodPolicy()
        s = _state(
            now=_weekend_at(23),
            is_workday_tomorrow=False,
            rce_high_price_threshold_gross=350.0,
        )
        # 80 netto × 1.23 = 98.4 gross < 350 threshold → free
        slots = _discharge_slots(morning_price_netto=80.0)
        assert p._compute_phase(s, slots) == Phase.NIGHT_FREE

    def test_night_at_3am(self):
        """Night phases also cover 00:00..07:00 (hour < 7 OR hour ≥ 22)."""
        p = DodPolicy()
        s = _state(now=_at(3), is_workday_tomorrow=True)
        assert p._compute_phase(s, _discharge_slots()) == Phase.NIGHT_PRESERVE

    def test_unknown_when_now_missing(self):
        p = DodPolicy()
        s = _state(now=_at(10))
        s.now = None
        assert p._compute_phase(s, _discharge_slots()) == Phase.UNKNOWN

    def test_unknown_when_workday_missing_in_morning(self):
        p = DodPolicy()
        s = _state(now=_at(8))
        s.is_workday = None
        assert p._compute_phase(s, _discharge_slots()) == Phase.UNKNOWN


class TestDirectPhasesDoD:
    """Phases with fixed DoD rule (no delegation, no entry initial)."""

    def test_afternoon_static_dod_zero(self):
        p = DodPolicy()
        s = _state(now=_at(14), rce_should_hold_for_peak=True)
        p.update(s, _battery_mgr(), _discharge_slots())
        assert p.target_dod == 0
        assert p.current_phase == Phase.AFTERNOON_STATIC

    def test_evening_dod_90(self):
        p = DodPolicy()
        s = _state(now=_at(20))
        p.update(s, _battery_mgr(), _discharge_slots())
        assert p.target_dod == 90

    def test_night_preserve_dod_zero(self):
        p = DodPolicy()
        s = _state(now=_at(23), is_workday_tomorrow=True)
        p.update(s, _battery_mgr(), _discharge_slots())
        assert p.target_dod == 0

    def test_night_free_dod_90(self):
        p = DodPolicy()
        s = _state(now=_weekend_at(23), is_workday_tomorrow=False)
        # 80 netto × 1.23 < 350 threshold → NIGHT_FREE → 90
        p.update(s, _battery_mgr(), _discharge_slots(morning_price_netto=80.0))
        assert p.target_dod == 90

    def test_weekend_morning_dod_zero(self):
        p = DodPolicy()
        s = _state(now=_weekend_at(9), is_workday=False)
        p.update(s, _battery_mgr(), _discharge_slots())
        assert p.target_dod == 0

    def test_ems_allow_discharge_dod_90(self):
        p = DodPolicy()
        s = _state(now=_at(10), ems_allow_discharge_override=True)
        p.update(
            s, _battery_mgr(block=True), _discharge_slots()
        )  # block ignored when EMS override
        assert p.target_dod == 90


class TestDelegatingPhasesDoD:
    """Phases that delegate DoD to BatteryManager.block_discharge."""

    def test_workday_pre_charge_block_true_dod_zero(self):
        p = DodPolicy()
        s = _state(now=_at(8))
        p.update(s, _battery_mgr(block=True), _discharge_slots())
        assert p.target_dod == 0
        assert p.current_phase == Phase.WORKDAY_PRE_CHARGE

    def test_workday_pre_charge_block_false_dod_90(self):
        p = DodPolicy()
        s = _state(now=_at(8))
        p.update(s, _battery_mgr(block=False), _discharge_slots())
        assert p.target_dod == 90

    def test_workday_post_charge_block_true_dod_zero(self):
        p = DodPolicy()
        s = _state(now=_at(11), start_charge=time(10, 0))
        p.update(s, _battery_mgr(block=True), _discharge_slots())
        assert p.target_dod == 0
        assert p.current_phase == Phase.WORKDAY_POST_CHARGE

    def test_workday_post_charge_block_false_dod_90(self):
        p = DodPolicy()
        s = _state(now=_at(11), start_charge=time(10, 0))
        p.update(s, _battery_mgr(block=False), _discharge_slots())
        assert p.target_dod == 90

    def test_afternoon_dynamic_block_true_dod_zero(self):
        p = DodPolicy()
        s = _state(now=_at(14), rce_should_hold_for_peak=False)
        p.update(s, _battery_mgr(block=True), _discharge_slots())
        assert p.target_dod == 0

    def test_afternoon_dynamic_block_false_dod_90(self):
        p = DodPolicy()
        s = _state(now=_at(14), rce_should_hold_for_peak=False)
        p.update(s, _battery_mgr(block=False), _discharge_slots())
        assert p.target_dod == 90


class TestOverride:
    """input_number.ems_dod_override ≥ 0 takes priority, expires on phase change."""

    def test_override_applied(self):
        p = DodPolicy()
        s = _state(now=_at(23), is_workday_tomorrow=True, dod_override=85)
        p.update(s, _battery_mgr(), _discharge_slots())
        assert p.target_dod == 85
        assert p._override_set_phase == Phase.NIGHT_PRESERVE

    def test_override_inactive_at_minus_one(self):
        p = DodPolicy()
        s = _state(now=_at(23), is_workday_tomorrow=True, dod_override=-1)
        p.update(s, _battery_mgr(), _discharge_slots())
        assert p.target_dod == 0  # NIGHT_PRESERVE direct rule
        assert p._override_set_phase is None

    def test_override_expires_on_phase_change(self):
        """Override expires when phase boundary is crossed.

        Set override at 23:00 (NIGHT_PRESERVE); tick at 07:30 enters
        WORKDAY_PRE_CHARGE (different phase) → override expired.
        """
        p = DodPolicy()
        # Tick 1: night, override=85
        s1 = _state(now=_at(23), is_workday_tomorrow=True, dod_override=85)
        p.update(s1, _battery_mgr(), _discharge_slots())
        assert p.target_dod == 85
        # Tick 2: morning, override still in input_number (user didn't reset)
        s2 = _state(now=_at(7, 30), is_workday=True, dod_override=85)
        p.update(s2, _battery_mgr(block=False), _discharge_slots())
        # Override expired (different phase) → delegate to BatteryManager.block
        assert p.target_dod == 90  # block=False → 90
        assert p._override_set_phase is None

    def test_override_persists_within_same_phase(self):
        p = DodPolicy()
        s = _state(now=_at(23), is_workday_tomorrow=True, dod_override=85)
        p.update(s, _battery_mgr(), _discharge_slots())
        # Tick 2: still night, override still 85
        s2 = _state(now=_at(23, 30), is_workday_tomorrow=True, dod_override=85)
        p.update(s2, _battery_mgr(), _discharge_slots())
        assert p.target_dod == 85
        assert p._override_set_phase == Phase.NIGHT_PRESERVE


class TestPersistence:
    """to_dict/from_dict roundtrip preserves state."""

    def test_roundtrip_with_override(self):
        original = DodPolicy(
            target_dod=85,
            current_phase=Phase.NIGHT_PRESERVE,
            _override_set_phase=Phase.NIGHT_PRESERVE,
        )
        d = original.to_dict()
        restored = DodPolicy.from_dict(d)
        assert restored.target_dod == 85
        assert restored.current_phase == Phase.NIGHT_PRESERVE
        assert restored._override_set_phase == Phase.NIGHT_PRESERVE

    def test_roundtrip_no_override(self):
        original = DodPolicy(
            target_dod=0,
            current_phase=Phase.AFTERNOON_STATIC,
            _override_set_phase=None,
        )
        d = original.to_dict()
        restored = DodPolicy.from_dict(d)
        assert restored.target_dod == 0
        assert restored.current_phase == Phase.AFTERNOON_STATIC
        assert restored._override_set_phase is None

    def test_from_empty_dict_uses_defaults(self):
        """Defensive: fresh install / corrupted store → safe defaults."""
        restored = DodPolicy.from_dict({})
        assert restored.target_dod == DEFAULT_DOD
        assert restored.current_phase == Phase.UNKNOWN
        assert restored._override_set_phase is None


class TestSelfHealingAfterRestart:
    """Persisted state + first tick post-restart should reach correct DoD."""

    def test_restart_in_pre_charge_no_re_emit_initial(self):
        """Restart still in same phase: no spurious entry-initial re-emit.

        Persisted current_phase=PRE_CHARGE; restart at 8:30 → still PRE_CHARGE
        → normal delegation (block=True → DoD=0), no re-emit of entry 90.
        """
        p = DodPolicy(current_phase=Phase.WORKDAY_PRE_CHARGE)
        s = _state(now=_at(8, 30))
        p.update(s, _battery_mgr(block=True), _discharge_slots())
        # Same phase as persisted → delegate (block=True → 0)
        assert p.target_dod == 0

    def test_restart_with_phase_change_delegates_immediately(self):
        """Restart that crosses phase boundary delegates to BatteryManager normally.

        Persisted current_phase=POST_CHARGE; restart at 13:30 → entered
        AFTERNOON_DYNAMIC → delegate to BatteryManager.block (no entry initial
        special case — BatteryManager hysteresis recomputes block correctly).
        """
        p = DodPolicy(current_phase=Phase.WORKDAY_POST_CHARGE)
        s = _state(now=_at(13, 30), rce_should_hold_for_peak=False)
        p.update(s, _battery_mgr(block=False), _discharge_slots())
        assert p.target_dod == 90  # block=False → delegate → 90
        assert p.current_phase == Phase.AFTERNOON_DYNAMIC


class TestWeekendBehavior:
    """User explicitly wants weekend morning DoD=0 (passive PV capture)."""

    def test_saturday_morning_dod_zero(self):
        p = DodPolicy()
        s = _state(now=_weekend_at(9, 24), is_workday=False)  # Saturday 9:24
        p.update(s, _battery_mgr(), _discharge_slots())
        assert p.target_dod == 0

    def test_saturday_afternoon_with_peak_dod_zero(self):
        p = DodPolicy()
        s = _state(now=_weekend_at(15), is_workday=False, rce_should_hold_for_peak=True)
        p.update(s, _battery_mgr(), _discharge_slots())
        assert p.target_dod == 0

    def test_saturday_afternoon_no_peak_delegates_to_block(self):
        """Weekend afternoon-dynamic: delegate to BatteryManager hysteresis.

        battery.py `_update_afternoon` runs hysteresis universally (no workday
        gate) — block depends on hourly export. block=False (typical weekend
        afternoon) → DoD=90.
        """
        p = DodPolicy()
        s = _state(
            now=_weekend_at(15), is_workday=False, rce_should_hold_for_peak=False
        )
        p.update(s, _battery_mgr(block=False), _discharge_slots())
        assert p.target_dod == 90
