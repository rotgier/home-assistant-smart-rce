"""Unit tests for the garden mowing planner domain (parity with legacy Jinja)."""

from dataclasses import asdict
from datetime import UTC, datetime, time, timedelta
import json

from custom_components.smart_rce.garden.domain.forecast_window import (
    ForecastSlot,
    WindowBound,
)
from custom_components.smart_rce.garden.domain.mowing_planner import (
    MowingInput,
    MowingPlanner,
    PlannerDecision,
    StartStrategy,
)
from custom_components.smart_rce.garden.domain.non_work import NonWorkHours

NOW = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)


def _slot(offset_min: int, prob: int, dur_min: int = 15) -> ForecastSlot:
    return ForecastSlot(
        start=NOW + timedelta(minutes=offset_min),
        rain_prob=prob,
        duration=timedelta(minutes=dur_min),
    )


def _dry_hours(count: int) -> list[ForecastSlot]:
    """`count` hours of dry 15-min slots starting at now."""
    return [_slot(i * 15, 0) for i in range(count * 4)]


def _decide(**kwargs: object) -> PlannerDecision:
    defaults: dict[str, object] = {
        "battery": 54,
        "progress": 45,
        "at_dock": True,
        "now": NOW,
        "slots": _dry_hours(6),
        "non_work": NonWorkHours(time(17, 0), time(10, 0)),
    }
    defaults.update(kwargs)
    return MowingPlanner().decide(MowingInput(**defaults))  # type: ignore[arg-type]


def test_rain_window_asap_starts_now() -> None:
    # Dry now, rain in 33 min: window (33) < needed (71) → start ASAP at start.
    slots = [_slot(0, 0), _slot(15, 0), _slot(30, 0), _slot(33, 60)]
    d = _decide(slots=slots)

    assert d.strategy is StartStrategy.ASAP
    assert d.window_bound is WindowBound.RAIN
    assert d.window_min == 33
    assert d.needed_min == 71
    assert d.opt_start == NOW
    assert d.window_end == NOW + timedelta(minutes=33)
    assert d.should_start is True


def test_long_window_battery_short_waits_to_charge() -> None:
    # Dry all day, window fits, but battery (drain 71) can't outlast the task
    # (finish 138) + reserve → WAIT_BATTERY: stay docked and charge, don't start.
    d = _decide()  # battery 54, progress 45

    assert d.strategy is StartStrategy.WAIT_BATTERY
    assert d.window_bound is WindowBound.NON_WORK
    assert d.needed_min == 71
    assert d.time_to_drain_min == 71
    assert d.time_to_finish_min == 138
    assert d.opt_start is None
    assert d.should_start is False


def test_long_window_battery_enough_go_starts_at_open() -> None:
    # Battery covers the task + reserve (drain 100 >= finish 50 + 10) and window
    # fits → GO starts at the window open (earliest), not deferred to the close.
    d = _decide(battery=70, progress=80)

    assert d.strategy is StartStrategy.GO
    assert d.window_bound is WindowBound.NON_WORK
    assert d.time_to_finish_min == 50
    assert d.time_to_drain_min == 100
    assert d.opt_start == NOW  # earliest: window open, not end-finish-buffer
    assert d.should_start is True


def test_battery_covers_task_but_not_reserve_waits() -> None:
    # drain 55 covers finish 50 but not the +10 reserve (55 < 60) → WAIT_BATTERY.
    d = _decide(battery=45, progress=80)

    assert d.time_to_drain_min == 55
    assert d.time_to_finish_min == 50
    assert d.strategy is StartStrategy.WAIT_BATTERY
    assert d.should_start is False


def test_tight_window_go_starts_at_open() -> None:
    # Battery enough (drain 100 >= finish 50 + 10); window just over finish (55)
    # → GO starts at the window open (same as any GO now — earliest possible).
    slots = [_slot(0, 0), _slot(15, 0), _slot(30, 0), _slot(45, 0), _slot(55, 60)]
    d = _decide(slots=slots, battery=70, progress=80)

    assert d.strategy is StartStrategy.GO
    assert d.time_to_finish_min == 50
    assert d.window_min == 55
    assert d.opt_start == NOW  # window open
    assert d.should_start is True


def test_quiet_end_grace_holds_start() -> None:
    # Window opens at the non-work end (10:00); Luba's firmware auto-resumes
    # there, so HA holds its start during RESUME_GRACE (10 min) to avoid racing
    # a duplicate cloud command. Strategy/opt_start unchanged — only the gate.
    now = datetime(2026, 6, 9, 10, 5, tzinfo=UTC)
    d = _decide(now=now, battery=70, progress=80)

    assert d.strategy is StartStrategy.GO
    assert d.opt_start == now  # earliest is still the window open (display)
    assert d.should_start is False  # but held by grace


def test_quiet_end_fallback_starts_after_grace() -> None:
    # Firmware didn't resume (still docked) past the grace → HA starts as fallback.
    now = datetime(2026, 6, 9, 10, 11, tzinfo=UTC)
    d = _decide(now=now, battery=70, progress=80)

    assert d.strategy is StartStrategy.GO
    assert d.should_start is True


def test_no_non_work_has_no_grace() -> None:
    # Without a non-work target there is no quiet end to race → no grace gate.
    now = datetime(2026, 6, 9, 10, 5, tzinfo=UTC)
    slots = [
        ForecastSlot(now, 0, timedelta(minutes=15)),
        ForecastSlot(now + timedelta(minutes=120), 60, timedelta(minutes=15)),
    ]
    d = _decide(now=now, battery=70, progress=80, slots=slots, non_work=None)

    assert d.strategy is StartStrategy.GO
    assert d.should_start is True


def test_short_window_skipped() -> None:
    # Rain in 20 min → window < WIN_MIN(30) → skip, never start.
    slots = [_slot(0, 0), _slot(20, 60)]
    d = _decide(slots=slots)

    assert d.strategy is StartStrategy.SKIP_SHORT_WINDOW
    assert d.window_min == 20
    assert d.opt_start is None
    assert d.should_start is False


def test_battery_below_min_does_not_start() -> None:
    slots = [_slot(0, 0), _slot(15, 0), _slot(30, 0), _slot(33, 60)]
    d = _decide(slots=slots, battery=29)

    assert d.should_start is False


def test_not_at_dock_does_not_start() -> None:
    # Identical to the ASAP-start case except the mower is not docked.
    slots = [_slot(0, 0), _slot(15, 0), _slot(30, 0), _slot(33, 60)]
    d = _decide(slots=slots, at_dock=False)

    assert d.strategy is StartStrategy.ASAP  # strategy still resolves
    assert d.opt_start == NOW
    assert d.should_start is False  # gated by at_dock


def test_window_end_is_start_of_next_rainy_slot() -> None:
    # now sits mid-slot in a dry bucket [0,15); rain begins exactly at +15 min.
    # The covering (dry) slot is not skipped as "end" — window runs to +15.
    slots = [_slot(0, 0), _slot(15, 60)]
    d = _decide(slots=slots, now=NOW + timedelta(minutes=1))

    assert d.window_start == NOW + timedelta(minutes=1)
    assert d.window_end == NOW + timedelta(minutes=15)
    assert d.window_bound is WindowBound.RAIN


def test_wet_now_window_starts_at_next_dry_slot() -> None:
    # Raining now, dry at +15 min, rain again at +30 min → window [15m, 30m].
    slots = [_slot(0, 80), _slot(15, 0), _slot(30, 80)]
    d = _decide(slots=slots)

    assert d.window_start == NOW + timedelta(minutes=15)
    assert d.window_end == NOW + timedelta(minutes=30)
    assert d.strategy is StartStrategy.SKIP_SHORT_WINDOW  # 15 min < WIN_MIN


def test_no_slot_covering_now_treated_as_dry() -> None:
    # Only a past slot exists → nothing covers now → dry; no future rain →
    # window clipped to non-work (5h). Here we only check the window geometry.
    slots = [_slot(-60, 80)]
    d = _decide(slots=slots, non_work=NonWorkHours(time(17, 0), time(10, 0)))

    assert d.window_start == NOW
    assert d.window_end == NOW + timedelta(hours=5)
    assert d.window_bound is WindowBound.NON_WORK


def test_needed_is_min_of_drain_and_finish() -> None:
    # Low battery → drain binds (9 min) below finish (225 min).
    d = _decide(battery=20, progress=10)

    assert d.time_to_drain_min == 9
    assert d.time_to_finish_min == 225
    assert d.needed_min == 9


def test_battery_at_floor_drains_zero() -> None:
    d = _decide(battery=15, progress=50)

    assert d.time_to_drain_min == 0
    assert d.needed_min == 0


def test_decision_serializes_for_ha_attributes() -> None:
    # Sensor layer uses asdict for extra_state_attributes; StrEnum serializes as
    # a plain string, datetime via HA's JSON encoder (here: default=str).
    d = _decide()
    payload = asdict(d)

    assert payload["strategy"] == "wait_battery"  # default fixture: battery short
    assert payload["window_bound"] == "non_work"
    assert "window_end" in payload  # renamed from `deadline`
    text = json.dumps(payload, default=str)
    assert '"strategy": "wait_battery"' in text


def test_time_left_overrides_linear_finish_and_flips_to_go() -> None:
    # Firmware says only 20 min left (vs linear 138 for progress 45). Battery
    # drain (71) now outlasts finish (20) + reserve (10) → GO, not WAIT_BATTERY.
    d = _decide(time_left_min=20)  # default fixture: battery 54, progress 45

    assert d.time_to_finish_min == 20
    assert d.needed_min == 20
    assert d.strategy is StartStrategy.GO
    assert d.should_start is True


def test_time_left_zero_falls_back_to_linear() -> None:
    # Sensor reports 0 (not a valid estimate) → linear PROGRESS_RATE fallback.
    d = _decide(time_left_min=0)

    assert d.time_to_finish_min == 138  # (100-45)/0.4
    assert d.strategy is StartStrategy.WAIT_BATTERY


def test_time_left_ignored_when_no_task() -> None:
    # progress == 0 → no task; time_left is meaningless, finish mirrors drain
    # (parity), regardless of any stale firmware value.
    d = _decide(progress=0, time_left_min=999)

    assert d.time_to_finish_min == d.time_to_drain_min


def test_fresh_wide_window_below_threshold_waits() -> None:
    # No task (progress 0), wide window, battery below the 90 threshold → charge.
    d = _decide(progress=0, battery=54)

    assert d.strategy is StartStrategy.WAIT_BATTERY
    assert d.should_start is False


def test_fresh_wide_window_at_threshold_goes() -> None:
    # Charged to the fresh-start threshold → GO at the window open (earliest).
    d = _decide(progress=0, battery=95)

    assert d.strategy is StartStrategy.GO
    assert d.opt_start == NOW
    assert d.should_start is True


def test_fresh_narrow_window_asap() -> None:
    # Window shorter than battery endurance → ASAP even below the threshold.
    slots = [_slot(0, 0), _slot(15, 0), _slot(30, 0), _slot(33, 60)]
    d = _decide(progress=0, battery=54, slots=slots)

    assert d.strategy is StartStrategy.ASAP
    assert d.should_start is True


def test_fresh_start_battery_threshold_tunable() -> None:
    # Lowering the threshold to 80 lets an 85% battery GO (default 90 would wait).
    assert _decide(progress=0, battery=85).strategy is StartStrategy.WAIT_BATTERY
    d = _decide(progress=0, battery=85, fresh_start_battery=80)

    assert d.strategy is StartStrategy.GO
    assert d.should_start is True


def test_fresh_start_not_held_by_quiet_grace() -> None:
    # A fresh start has no in-progress task to auto-resume, so RESUME_GRACE does
    # not hold it: it fires right at the quiet end (cf. test_quiet_end_grace which
    # holds a resume at the same 10:05).
    now = datetime(2026, 6, 9, 10, 5, tzinfo=UTC)
    d = _decide(now=now, progress=0, battery=95)

    assert d.strategy is StartStrategy.GO
    assert d.should_start is True
