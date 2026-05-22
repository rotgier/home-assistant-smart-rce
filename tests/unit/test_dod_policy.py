"""Unit tests for DodPolicy — phase dispatch + hysteresis + override + persistence."""

from datetime import datetime, time

from custom_components.smart_rce.domain.dod_policy import DEFAULT_DOD, DodPolicy, Phase
from custom_components.smart_rce.domain.input_state import InputState
from custom_components.smart_rce.domain.rce import TIMEZONE

DEFAULT_START_CHARGE: time = time(10, 0)


def _state(
    *,
    now,
    is_workday=True,
    is_workday_tomorrow=True,
    rce_should_hold_for_peak=False,
    dod_override=-1.0,
    exported_energy_hourly: float | None = 0.0,
    pv_available_5min: float | None = None,
) -> InputState:
    # Note: `ems_interventions_blocked` (Etap 0) and `start_charge_hour_override`
    # (Etap B'-2) were removed from InputState. Tests exercising those paths
    # pass them as kwargs to `_update(policy, state, ...)` directly.
    return InputState(
        now=now,
        is_workday=is_workday,
        is_workday_tomorrow=is_workday_tomorrow,
        rce_should_hold_for_peak=rce_should_hold_for_peak,
        dod_override=dod_override,
        exported_energy_hourly=exported_energy_hourly,
        consumption_minus_pv_5_minutes=(
            -pv_available_5min if pv_available_5min is not None else None
        ),
    )


def _update(policy: DodPolicy, state: InputState, **kwargs) -> None:
    """Call policy.update with default start_charge_hour_override applied."""
    kwargs.setdefault("start_charge_hour_override", DEFAULT_START_CHARGE)
    DodPolicy.update(policy, state, **kwargs)


def _at(h, m=0):
    return datetime(2026, 5, 11, h, m, tzinfo=TIMEZONE)  # 2026-05-11 = Monday (workday)


def _weekend_at(h, m=0):
    return datetime(2026, 5, 9, h, m, tzinfo=TIMEZONE)  # 2026-05-09 = Saturday


class TestPhaseDispatch:
    """_compute_phase returns correct phase for time + flags combinations."""

    def test_ems_interventions_blocked_overrides_all(self):
        p = DodPolicy()
        s = _state(now=_at(10))
        assert (
            p._compute_phase(s, ems_interventions_blocked=True)
            == Phase.INTERVENTIONS_BLOCKED
        )

    def test_workday_pre_charge(self):
        p = DodPolicy()
        s = _state(now=_at(8), is_workday=True)
        assert (
            p._compute_phase(s, start_charge_hour_override=time(10, 0))
            == Phase.WORKDAY_PRE_CHARGE
        )

    def test_workday_post_charge(self):
        p = DodPolicy()
        s = _state(now=_at(11), is_workday=True)
        assert (
            p._compute_phase(s, start_charge_hour_override=time(10, 0))
            == Phase.WORKDAY_POST_CHARGE
        )

    def test_weekend_morning(self):
        p = DodPolicy()
        s = _state(now=_weekend_at(9), is_workday=False)
        assert p._compute_phase(s) == Phase.WEEKEND_MORNING

    def test_weekend_morning_at_7_sharp(self):
        p = DodPolicy()
        s = _state(now=_weekend_at(7), is_workday=False)
        assert p._compute_phase(s) == Phase.WEEKEND_MORNING

    def test_afternoon_static_peak(self):
        p = DodPolicy()
        s = _state(now=_at(14), rce_should_hold_for_peak=True)
        assert p._compute_phase(s) == Phase.AFTERNOON_STATIC

    def test_afternoon_dynamic_no_peak(self):
        p = DodPolicy()
        s = _state(now=_at(14), rce_should_hold_for_peak=False)
        assert p._compute_phase(s) == Phase.AFTERNOON_DYNAMIC

    def test_afternoon_static_applies_on_weekend_too(self):
        p = DodPolicy()
        s = _state(now=_weekend_at(15), is_workday=False, rce_should_hold_for_peak=True)
        assert p._compute_phase(s) == Phase.AFTERNOON_STATIC

    def test_evening_workday_discharge(self):
        """Workday evening → DISCHARGE (cover expensive consumption)."""
        p = DodPolicy()
        s = _state(now=_at(20), is_workday=True)
        assert p._compute_phase(s) == Phase.EVENING_DISCHARGE

    def test_evening_weekend_with_peak_preserve(self):
        """Weekend evening + hold_for_peak=True → PRESERVE."""
        p = DodPolicy()
        s = _state(now=_weekend_at(20), is_workday=False, rce_should_hold_for_peak=True)
        assert p._compute_phase(s) == Phase.EVENING_PRESERVE

    def test_evening_weekend_workday_tomorrow_preserve(self):
        """Weekend evening + workday_tomorrow → PRESERVE (morning load)."""
        p = DodPolicy()
        s = _state(
            now=_weekend_at(20),
            is_workday=False,
            is_workday_tomorrow=True,
            rce_should_hold_for_peak=False,
        )
        assert p._compute_phase(s) == Phase.EVENING_PRESERVE

    def test_evening_weekend_no_peak_no_workday_tomorrow_discharge(self):
        """Weekend evening + no peak + weekend tomorrow → DISCHARGE (free)."""
        p = DodPolicy()
        s = _state(
            now=_weekend_at(20),
            is_workday=False,
            is_workday_tomorrow=False,
            rce_should_hold_for_peak=False,
        )
        assert p._compute_phase(s) == Phase.EVENING_DISCHARGE

    def test_evening_weekend_workday_tomorrow_none_unknown(self):
        """Weekend evening + workday_tomorrow=None → UNKNOWN (defensive)."""
        p = DodPolicy()
        s = _state(
            now=_weekend_at(20),
            is_workday=False,
            rce_should_hold_for_peak=False,
        )
        s.is_workday_tomorrow = None
        assert p._compute_phase(s) == Phase.UNKNOWN

    def test_night_preserve_workday_tomorrow(self):
        p = DodPolicy()
        s = _state(now=_at(23), is_workday_tomorrow=True)
        assert p._compute_phase(s) == Phase.NIGHT_PRESERVE

    def test_night_free_weekend_tomorrow(self):
        """Weekend tomorrow → NIGHT_FREE (no morning load)."""
        p = DodPolicy()
        s = _state(now=_weekend_at(23), is_workday_tomorrow=False)
        assert p._compute_phase(s) == Phase.NIGHT_FREE

    def test_night_unknown_when_workday_tomorrow_missing(self):
        """workday_tomorrow=None at night → UNKNOWN keep-state."""
        p = DodPolicy()
        s = _state(now=_at(23))
        s.is_workday_tomorrow = None
        assert p._compute_phase(s) == Phase.UNKNOWN

    def test_night_at_3am(self):
        """Night phases also cover 00:00..07:00 (hour < 7 OR hour ≥ 22)."""
        p = DodPolicy()
        s = _state(now=_at(3), is_workday_tomorrow=True)
        assert p._compute_phase(s) == Phase.NIGHT_PRESERVE

    def test_unknown_when_now_missing(self):
        p = DodPolicy()
        s = _state(now=_at(10))
        s.now = None
        assert p._compute_phase(s) == Phase.UNKNOWN

    def test_unknown_when_workday_missing_in_morning(self):
        p = DodPolicy()
        s = _state(now=_at(8))
        s.is_workday = None
        assert p._compute_phase(s) == Phase.UNKNOWN


class TestDirectPhasesDoD:
    """Phases with fixed DoD rule (no delegation, no entry initial)."""

    def test_afternoon_static_dod_zero(self):
        p = DodPolicy()
        s = _state(now=_at(14), rce_should_hold_for_peak=True)
        _update(p, s)
        assert p.target_dod == 0
        assert p.current_phase == Phase.AFTERNOON_STATIC

    def test_evening_workday_dod_90(self):
        p = DodPolicy()
        s = _state(now=_at(20), is_workday=True)
        _update(p, s)
        assert p.target_dod == 90
        assert p.current_phase == Phase.EVENING_DISCHARGE

    def test_evening_weekend_preserve_dod_zero(self):
        """Weekend evening + workday_tomorrow → DoD=0."""
        p = DodPolicy()
        s = _state(
            now=_weekend_at(20),
            is_workday=False,
            is_workday_tomorrow=True,
            rce_should_hold_for_peak=False,
        )
        _update(p, s)
        assert p.target_dod == 0
        assert p.current_phase == Phase.EVENING_PRESERVE

    def test_night_preserve_dod_zero(self):
        p = DodPolicy()
        s = _state(now=_at(23), is_workday_tomorrow=True)
        _update(p, s)
        assert p.target_dod == 0

    def test_night_free_dod_90(self):
        p = DodPolicy()
        s = _state(now=_weekend_at(23), is_workday_tomorrow=False)
        _update(p, s)
        assert p.target_dod == 90

    def test_weekend_morning_dod_zero(self):
        p = DodPolicy()
        s = _state(now=_weekend_at(9), is_workday=False)
        _update(p, s)
        assert p.target_dod == 0

    def test_interventions_blocked_dod_90(self):
        p = DodPolicy()
        s = _state(now=_at(10))
        _update(p, s, ems_interventions_blocked=True)  # block ignored when EMS override
        assert p.target_dod == 90


class TestDelegatingPhasesDoD:
    """Delegating phases call block_discharge fn → DoD = 0 if block else 90.

    Hysteresis algorithms tested in test_block_discharge.py — these tests
    smoke-check the dispatch + block→DoD mapping per delegating phase.
    """

    # --- WORKDAY_PRE_CHARGE --- #
    def test_workday_pre_charge_high_export_dod_zero(self):
        """Exported >=100 Wh → SET → DoD=0."""
        p = DodPolicy()
        s = _state(now=_at(8), exported_energy_hourly=0.150)
        _update(p, s)
        assert p.target_dod == 0
        assert p.current_phase == Phase.WORKDAY_PRE_CHARGE
        assert p._prev_block is True

    def test_workday_pre_charge_low_export_dod_90(self):
        """Exported < 50 + no surplus → default reset → DoD=90."""
        p = DodPolicy()
        s = _state(now=_at(8), exported_energy_hourly=0.0)
        _update(p, s)
        assert p.target_dod == 90
        assert p._prev_block is False

    # --- WORKDAY_POST_CHARGE --- #
    def test_workday_post_charge_instant_surplus_dod_zero(self):
        """pv_5min > +500W → instant_surplus SET → DoD=0."""
        p = DodPolicy()
        s = _state(now=_at(11), pv_available_5min=700.0)
        _update(p, s)
        assert p.target_dod == 0
        assert p.current_phase == Phase.WORKDAY_POST_CHARGE

    def test_workday_post_charge_deficit_and_reset_dod_90(self):
        """instant_deficit + hourly_reset → RESET → DoD=90."""
        p = DodPolicy(_prev_block=True)
        s = _state(
            now=_at(11),
            exported_energy_hourly=0.030,
            pv_available_5min=-100.0,
        )
        _update(p, s)
        assert p.target_dod == 90

    # --- AFTERNOON_DYNAMIC --- #
    def test_afternoon_dynamic_net_export_dod_zero(self):
        """hourly_export > 0 → SET → DoD=0."""
        p = DodPolicy()
        s = _state(
            now=_at(14), rce_should_hold_for_peak=False, exported_energy_hourly=0.030
        )
        _update(p, s)
        assert p.target_dod == 0

    def test_afternoon_dynamic_deficit_no_export_dod_90(self):
        """instant_deficit + no hourly_export → RESET → DoD=90."""
        p = DodPolicy(_prev_block=True)
        s = _state(
            now=_at(14),
            rce_should_hold_for_peak=False,
            exported_energy_hourly=0.0,
            pv_available_5min=-100.0,
        )
        _update(p, s)
        assert p.target_dod == 90


class TestPrevBlockHysteresisKeepState:
    """_prev_block carried across ticks — hysteresis dead-zone keeps state."""

    def test_pre_charge_dead_zone_keeps_block_true(self):
        """Exported in dead zone (50..100) → keep prev_block=True."""
        p = DodPolicy(_prev_block=True)
        s = _state(now=_at(8), exported_energy_hourly=0.060)
        _update(p, s)
        assert p._prev_block is True
        assert p.target_dod == 0

    def test_pre_charge_dead_zone_keeps_block_false(self):
        """Exported in dead zone (50..100) + prev=False → stays False."""
        p = DodPolicy(_prev_block=False)
        s = _state(now=_at(8), exported_energy_hourly=0.060)
        _update(p, s)
        assert p._prev_block is False
        assert p.target_dod == 90

    def test_pre_charge_instant_surplus_extends_keep_zone(self):
        """Exported < 50 + instant_surplus (>500W) → keep prev=True."""
        p = DodPolicy(_prev_block=True)
        s = _state(now=_at(8), exported_energy_hourly=0.020, pv_available_5min=800.0)
        _update(p, s)
        assert p._prev_block is True


class TestPrevBlockSyncOnDirectPhase:
    """Direct phase (e.g. AFTERNOON_STATIC, NIGHT_PRESERVE) syncs _prev_block."""

    def test_direct_dod_zero_sets_prev_block_true(self):
        """AFTERNOON_STATIC (DoD=0) syncs _prev_block=True for next delegating tick."""
        p = DodPolicy(_prev_block=False)
        s = _state(now=_at(14), rce_should_hold_for_peak=True)
        _update(p, s)
        assert p._prev_block is True

    def test_direct_dod_90_sets_prev_block_false(self):
        """EVENING (DoD=90) syncs _prev_block=False."""
        p = DodPolicy(_prev_block=True)
        s = _state(now=_at(20))
        _update(p, s)
        assert p._prev_block is False


class TestUnknownPhaseKeepState:
    """UNKNOWN phase (inputs missing) preserves persisted target_dod."""

    def test_unknown_keeps_target_dod(self):
        """now=None → UNKNOWN → don't overwrite target_dod."""
        p = DodPolicy(target_dod=42, current_phase=Phase.WORKDAY_PRE_CHARGE)
        s = _state(now=_at(8))
        s.now = None
        _update(p, s)
        assert p.target_dod == 42
        assert p.current_phase == Phase.WORKDAY_PRE_CHARGE  # also kept

    def test_unknown_keeps_prev_block(self):
        p = DodPolicy(target_dod=0, _prev_block=True)
        s = _state(now=_at(8))
        s.is_workday = None  # morning + workday=None → UNKNOWN
        _update(p, s)
        assert p._prev_block is True


class TestOverride:
    """input_number.ems_dod_override ≥ 0 takes priority, expires on phase change."""

    def test_override_applied(self):
        p = DodPolicy()
        s = _state(now=_at(23), is_workday_tomorrow=True, dod_override=85)
        _update(p, s)
        assert p.target_dod == 85
        assert p._override_set_phase == Phase.NIGHT_PRESERVE

    def test_override_inactive_at_minus_one(self):
        p = DodPolicy()
        s = _state(now=_at(23), is_workday_tomorrow=True, dod_override=-1)
        _update(p, s)
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
        _update(p, s1)
        assert p.target_dod == 85
        # Tick 2: morning, override still in input_number (user didn't reset)
        s2 = _state(now=_at(7, 30), is_workday=True, dod_override=85)
        _update(p, s2)
        # Override expired (different phase) → delegate to BatteryManager.block
        assert p.target_dod == 90  # block=False → 90
        assert p._override_set_phase is None

    def test_override_persists_within_same_phase(self):
        p = DodPolicy()
        s = _state(now=_at(23), is_workday_tomorrow=True, dod_override=85)
        _update(p, s)
        # Tick 2: still night, override still 85
        s2 = _state(now=_at(23, 30), is_workday_tomorrow=True, dod_override=85)
        _update(p, s2)
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

        Persisted current_phase=PRE_CHARGE; restart at 8:30 with sustained
        export → block=True → DoD=0 via hysteresis (no entry-initial 90).
        """
        p = DodPolicy(current_phase=Phase.WORKDAY_PRE_CHARGE)
        s = _state(now=_at(8, 30), exported_energy_hourly=0.150)
        _update(p, s)
        assert p.target_dod == 0

    def test_restart_with_phase_change_delegates_immediately(self):
        """Restart that crosses phase boundary delegates to hysteresis normally.

        Persisted current_phase=POST_CHARGE; restart at 13:30 → enters
        AFTERNOON_DYNAMIC. Default state (no export, no instant signal) →
        block_afternoon_dynamic keeps prev=False → DoD=90.
        """
        p = DodPolicy(current_phase=Phase.WORKDAY_POST_CHARGE)
        s = _state(now=_at(13, 30), rce_should_hold_for_peak=False)
        _update(p, s)
        assert p.target_dod == 90
        assert p.current_phase == Phase.AFTERNOON_DYNAMIC


class TestWeekendBehavior:
    """User explicitly wants weekend morning DoD=0 (passive PV capture)."""

    def test_saturday_morning_dod_zero(self):
        p = DodPolicy()
        s = _state(now=_weekend_at(9, 24), is_workday=False)  # Saturday 9:24
        _update(p, s)
        assert p.target_dod == 0

    def test_saturday_afternoon_with_peak_dod_zero(self):
        p = DodPolicy()
        s = _state(now=_weekend_at(15), is_workday=False, rce_should_hold_for_peak=True)
        _update(p, s)
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
        _update(p, s)
        assert p.target_dod == 90
