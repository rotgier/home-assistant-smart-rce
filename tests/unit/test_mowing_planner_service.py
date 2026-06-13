"""Unit tests for MowingPlannerService (orchestration, non_work source, notify)."""

from datetime import datetime, time, timedelta
from unittest.mock import MagicMock

from custom_components.smart_rce.garden.application.mowing_planner_service import (
    MowingPlannerService,
)
from custom_components.smart_rce.garden.domain.mowing_planner import StartStrategy
from custom_components.smart_rce.garden.domain.non_work import NonWorkHours
from custom_components.smart_rce.garden.infrastructure.forecast_reader import (
    parse_forecast_slots,
)

NOW = datetime(2026, 6, 12, 12, 0)


def _forecast_hourly(rain_prob: int = 0) -> list[dict]:
    """Six dry/wet hours starting at NOW."""
    return [
        {
            "datetime": (NOW + timedelta(hours=i)).isoformat(),
            "precipitation_probability": rain_prob,
        }
        for i in range(6)
    ]


def _service(
    *,
    battery: int = 80,
    progress: int = 0,
    at_dock: bool = True,
    forecast: list[dict] | None = None,  # raw HA shape — parsed in the fixture
    effective: NonWorkHours | None = NonWorkHours(time(20, 35), time(10, 5)),
    dry_at: datetime | None = None,
    now: datetime = NOW,
) -> MowingPlannerService:
    luba = MagicMock()
    luba.read_battery.return_value = battery
    luba.read_progress.return_value = progress
    luba.read_at_dock.return_value = at_dock
    forecast_reader = MagicMock()
    forecast_reader.read_forecast_slots.return_value = parse_forecast_slots(
        forecast if forecast is not None else _forecast_hourly()
    )
    non_work = MagicMock()
    non_work.effective_hours = effective
    rain = MagicMock()
    rain.dry_at = dry_at
    return MowingPlannerService(luba, forecast_reader, non_work, rain, lambda: now)


def test_recompute_produces_decision() -> None:
    service = _service()

    service.recompute()

    decision = service.decision
    assert decision is not None
    assert decision.battery == 80
    assert decision.at_dock is True


def test_non_work_target_clips_window_today() -> None:
    service = _service()  # target 20:35, now 12:00 → non_work today 20:35

    service.recompute()

    assert service.decision is not None
    assert service.decision.window_end == NOW.replace(hour=20, minute=35)


def test_non_work_start_rolls_to_tomorrow_when_past() -> None:
    late = NOW.replace(hour=21, minute=0)  # past 20:35 → tomorrow
    service = _service(now=late)

    service.recompute()

    assert service.decision is not None
    assert service.decision.window_end == late.replace(hour=20, minute=35) + timedelta(
        days=1
    )


def test_effective_hours_drive_the_window() -> None:
    # Source selection (target vs cloud fallback) is NonWorkService's concern —
    # the planner just consumes whatever effective_hours says.
    service = _service(effective=NonWorkHours(time(19, 0), time(9, 0)))

    service.recompute()

    assert service.decision is not None
    assert service.decision.window_end == NOW.replace(hour=19, minute=0)


def test_no_non_work_source_means_unbounded_window() -> None:
    service = _service(effective=None)

    service.recompute()

    assert service.decision is not None
    # Dry forecast, no rain, no non_work → no end → NO_WINDOW strategy.
    assert service.decision.strategy is StartStrategy.NO_WINDOW


def test_notifies_only_on_decision_change() -> None:
    service = _service()
    notified: list[int] = []
    service.add_listener(lambda: notified.append(1))

    service.recompute()
    service.recompute()  # same inputs, same minute → same decision

    assert len(notified) == 1


def test_inside_quiet_hours_window_opens_at_quiet_end() -> None:
    # 22:00, quiet 20:35-10:05 → window cannot open before tomorrow 10:05.
    late = NOW.replace(hour=22, minute=0)
    forecast = [
        {
            "datetime": (late + timedelta(hours=i)).isoformat(),
            "precipitation_probability": 0,
        }
        for i in range(16)
    ]
    service = _service(now=late, forecast=forecast)

    service.recompute()

    decision = service.decision
    assert decision is not None
    assert decision.should_start is False  # legacy+patch parity: no night alert
    assert decision.window_start == late.replace(hour=10, minute=5) + timedelta(days=1)


def test_daytime_outside_quiet_hours_window_opens_now() -> None:
    service = _service()  # 12:00, quiet 20:35-10:05 → outside

    service.recompute()

    assert service.decision is not None
    assert service.decision.window_start == NOW


def test_dry_at_floor_delays_window_start() -> None:
    # Dry now per forecast, but grass dry-out (dry_at) is 2h out → window
    # cannot start before dry_at.
    dry_at = NOW + timedelta(hours=2)
    service = _service(dry_at=dry_at)

    service.recompute()

    assert service.decision is not None
    assert service.decision.window_start == dry_at
