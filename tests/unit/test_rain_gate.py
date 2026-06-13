"""Unit tests for the rain gate — RainGate domain + RainGateService push logic."""

from datetime import UTC, datetime, time, timedelta
from unittest.mock import MagicMock

from custom_components.smart_rce.garden.application.rain_gate_service import (
    RainGateService,
)
from custom_components.smart_rce.garden.domain.non_work import NonWorkHours
from custom_components.smart_rce.garden.domain.rain_gate import RainGate

TARGET = NonWorkHours(time(20, 35), time(10, 5))
# 09:58 is inside the 20:35→10:05 window and within 10 min of the 10:05 end.
NEAR = datetime(2026, 6, 13, 9, 58, tzinfo=UTC)
TARGET_END = datetime(2026, 6, 13, 10, 5, tzinfo=UTC)


# --- RainGate domain ---


def test_idle_when_outside_window_and_not_holding() -> None:
    gate = RainGate()
    assert gate.evaluate(NEAR, None, False, None, 5.0) is False
    assert gate.hold_until is None


def test_not_near_boundary_leaves_hold_untouched() -> None:
    gate = RainGate()
    far = datetime(2026, 6, 13, 9, 0, tzinfo=UTC)  # 65 min before 10:05
    assert gate.evaluate(far, TARGET_END, True, None, 5.0) is False
    assert gate.hold_until is None


def test_near_boundary_wet_extends_by_dry_hours() -> None:
    gate = RainGate()
    assert gate.evaluate(NEAR, TARGET_END, True, None, 5.0) is True
    assert gate.hold_until == NEAR + timedelta(hours=5)


def test_near_boundary_dry_at_future_holds_until_dry_at() -> None:
    gate = RainGate()
    dry_at = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)
    assert gate.evaluate(NEAR, TARGET_END, False, dry_at, 5.0) is True
    assert gate.hold_until == dry_at


def test_near_boundary_dry_does_not_hold() -> None:
    gate = RainGate()
    dry_at = datetime(2026, 6, 13, 9, 0, tzinfo=UTC)  # already in the past
    assert gate.evaluate(NEAR, TARGET_END, False, dry_at, 5.0) is False
    assert gate.hold_until is None


def test_holding_then_dry_clears_to_restore() -> None:
    gate = RainGate(hold_until=datetime(2026, 6, 13, 14, 55, tzinfo=UTC))
    now = datetime(2026, 6, 13, 14, 50, tzinfo=UTC)  # near the extended end
    assert gate.evaluate(now, None, False, None, 5.0) is True
    assert gate.hold_until is None


def test_holding_still_wet_re_extends_from_current_end() -> None:
    gate = RainGate(hold_until=datetime(2026, 6, 13, 14, 55, tzinfo=UTC))
    now = datetime(2026, 6, 13, 14, 50, tzinfo=UTC)
    assert gate.evaluate(now, None, True, None, 5.0) is True
    assert gate.hold_until == now + timedelta(hours=5)


# --- RainGateService ---


def _service(
    *,
    target: NonWorkHours | None = TARGET,
    wet: bool = False,
    dry_at: datetime | None = None,
    dry_hours: float = 5.0,
    now: datetime = NEAR,
) -> tuple[RainGateService, MagicMock, MagicMock]:
    non_work = MagicMock()
    non_work.effective_hours = target
    rain = MagicMock()
    rain.currently_wet = wet
    rain.dry_at = dry_at
    rain.dry_hours = dry_hours
    actuator = MagicMock()
    actuator.apply = MagicMock(return_value="coro")  # not awaited — handed to tasks
    tasks = MagicMock()
    service = RainGateService(non_work, rain, actuator, tasks, lambda: now)
    return service, actuator, tasks


def test_service_no_target_does_nothing() -> None:
    service, actuator, tasks = _service(target=None)
    service.evaluate()
    tasks.run_background.assert_not_called()
    actuator.apply.assert_not_called()


def test_service_extends_pushes_and_notifies() -> None:
    service, actuator, tasks = _service(wet=True)
    notified: list[int] = []
    service.add_listener(lambda: notified.append(1))

    service.evaluate()

    assert service.hold_until == NEAR + timedelta(hours=5)
    actuator.apply.assert_called_once_with(NonWorkHours(time(20, 35), time(14, 58)))
    tasks.run_background.assert_called_once()
    assert notified == [1]


def test_service_dry_no_change_no_push() -> None:
    service, actuator, tasks = _service(wet=False)
    service.evaluate()
    tasks.run_background.assert_not_called()
    actuator.apply.assert_not_called()


def test_service_restore_pushes_target() -> None:
    now = datetime(2026, 6, 13, 14, 50, tzinfo=UTC)
    service, actuator, tasks = _service(wet=False, now=now)
    service._gate.hold_until = datetime(2026, 6, 13, 14, 55, tzinfo=UTC)  # noqa: SLF001

    service.evaluate()

    assert service.hold_until is None
    actuator.apply.assert_called_once_with(TARGET)
    tasks.run_background.assert_called_once()
