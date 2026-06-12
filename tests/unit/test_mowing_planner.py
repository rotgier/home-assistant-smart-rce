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
    assert d.deadline == NOW + timedelta(minutes=33)
    assert d.should_start is True


def test_long_window_lazy_start_not_yet() -> None:
    # Dry all day, window clipped to non-work: window (300) >= needed → lazy
    # start at end-needed, still in the future → don't start yet.
    d = _decide()

    assert d.strategy is StartStrategy.LAZY
    assert d.window_bound is WindowBound.NON_WORK
    assert d.needed_min == 71
    assert d.time_to_finish_min == 138
    assert d.opt_start == NOW + timedelta(hours=5) - timedelta(minutes=71)
    assert d.should_start is False


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
    assert d.deadline == NOW + timedelta(minutes=15)
    assert d.window_bound is WindowBound.RAIN


def test_wet_now_window_starts_at_next_dry_slot() -> None:
    # Raining now, dry at +15 min, rain again at +30 min → window [15m, 30m].
    slots = [_slot(0, 80), _slot(15, 0), _slot(30, 80)]
    d = _decide(slots=slots)

    assert d.window_start == NOW + timedelta(minutes=15)
    assert d.deadline == NOW + timedelta(minutes=30)
    assert d.strategy is StartStrategy.SKIP_SHORT_WINDOW  # 15 min < WIN_MIN


def test_no_slot_covering_now_treated_as_dry() -> None:
    # Only a past slot exists → nothing covers now → dry; no future rain →
    # window clipped to non-work (5h, longer than needed → lazy).
    slots = [_slot(-60, 80)]
    d = _decide(slots=slots, non_work=NonWorkHours(time(17, 0), time(10, 0)))

    assert d.window_start == NOW
    assert d.deadline == NOW + timedelta(hours=5)
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

    assert payload["strategy"] == "lazy"
    assert payload["window_bound"] == "non_work"
    text = json.dumps(payload, default=str)
    assert '"strategy": "lazy"' in text
