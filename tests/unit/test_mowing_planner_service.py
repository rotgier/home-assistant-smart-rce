"""Unit tests for MowingPlannerService (orchestration, non_work source, notify)."""

from datetime import datetime, time, timedelta
from unittest.mock import MagicMock

from custom_components.smart_rce.garden.application.mowing_planner_service import (
    MowingPlannerService,
)
from custom_components.smart_rce.garden.domain.mowing_planner import StartStrategy
from custom_components.smart_rce.garden.domain.non_work import NonWorkHours

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
    forecast: list[dict] | None = None,
    target_start: time | None = time(20, 35),
    cloud: NonWorkHours | None = None,
    now: datetime = NOW,
) -> MowingPlannerService:
    luba = MagicMock()
    luba.read_battery.return_value = battery
    luba.read_progress.return_value = progress
    luba.read_at_dock.return_value = at_dock
    forecast_provider = MagicMock()
    forecast_provider.forecast_hourly = (
        forecast if forecast is not None else _forecast_hourly()
    )
    non_work = MagicMock()
    non_work.start = target_start
    fallback = MagicMock()
    fallback.read_non_work_hours.return_value = cloud
    return MowingPlannerService(
        luba, forecast_provider, non_work, fallback, lambda: now
    )


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
    assert service.decision.deadline == NOW.replace(hour=20, minute=35)


def test_non_work_start_rolls_to_tomorrow_when_past() -> None:
    late = NOW.replace(hour=21, minute=0)  # past 20:35 → tomorrow
    service = _service(now=late)

    service.recompute()

    assert service.decision is not None
    assert service.decision.deadline == late.replace(hour=20, minute=35) + timedelta(
        days=1
    )


def test_fallback_to_cloud_when_target_unset() -> None:
    service = _service(target_start=None, cloud=NonWorkHours(time(19, 0), time(9, 0)))

    service.recompute()

    assert service.decision is not None
    assert service.decision.deadline == NOW.replace(hour=19, minute=0)


def test_no_non_work_source_means_unbounded_window() -> None:
    service = _service(target_start=None, cloud=None)

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
