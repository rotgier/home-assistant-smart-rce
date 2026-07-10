"""Unit tests for the rain gate — RainGate domain + RainGateService push logic."""

from datetime import UTC, datetime, time
from unittest.mock import MagicMock

from custom_components.smart_rce.garden.application.rain_gate_service import (
    RainGateService,
)
from custom_components.smart_rce.garden.domain.non_work import NonWorkHours
from custom_components.smart_rce.garden.domain.rain_gate import RainGate

TARGET = NonWorkHours(time(20, 35), time(10, 5))  # quiet 20:35 → 10:05
NEAR = datetime(2026, 6, 13, 9, 58, tzinfo=UTC)  # inside quiet, ≤10 min to 10:05 end
WORK = datetime(2026, 6, 13, 16, 31, tzinfo=UTC)  # working hours (10:05–20:35)


def _dt(hour: int, minute: int) -> datetime:
    return datetime(2026, 6, 13, hour, minute, tzinfo=UTC)


# --- RainGate domain ---


def test_idle_when_no_target() -> None:
    gate = RainGate()
    assert gate.evaluate(NEAR, None, None, False) is False
    assert gate.override is None


def test_dry_clears() -> None:
    gate = RainGate(override=NonWorkHours(time(16, 31), time(19, 31)))
    assert gate.evaluate(_dt(20, 10), TARGET, _dt(19, 55), True) is True  # dry_at past
    assert gate.override is None


# inside the quiet window — morning-boundary end-extension


def test_not_near_boundary_leaves_override_untouched() -> None:
    gate = RainGate()
    far = _dt(9, 0)  # 65 min before the 10:05 end
    assert gate.evaluate(far, TARGET, _dt(12, 0), False) is False
    assert gate.override is None


def test_near_boundary_extends_end_to_dry_at() -> None:
    gate = RainGate()
    assert gate.evaluate(NEAR, TARGET, _dt(12, 0), False) is True
    assert gate.override == NonWorkHours(time(20, 35), time(12, 0))


def test_near_boundary_dry_at_within_target_end_does_not_hold() -> None:
    gate = RainGate()
    assert gate.evaluate(NEAR, TARGET, _dt(10, 0), False) is False  # dry_at ≤ 10:05
    assert gate.override is None


# working hours — mid-day charge-resume block


def test_working_hours_docked_with_task_blocks_until_dry_at() -> None:
    gate = RainGate()
    assert gate.evaluate(WORK, TARGET, _dt(19, 31), True) is True
    assert gate.override == NonWorkHours(time(16, 31), time(19, 31))


def test_working_hours_not_docked_does_not_block() -> None:
    gate = RainGate()
    assert gate.evaluate(WORK, TARGET, _dt(19, 31), False) is False
    assert gate.override is None


def test_block_pins_start_antichurn_then_extends() -> None:
    gate = RainGate()
    assert gate.evaluate(WORK, TARGET, _dt(19, 31), True) is True
    assert gate.override == NonWorkHours(time(16, 31), time(19, 31))
    # dry_at crept +5 min (< REWRITE_MARGIN) → no rewrite, start pinned
    assert gate.evaluate(_dt(16, 36), TARGET, _dt(19, 36), True) is False
    assert gate.override == NonWorkHours(time(16, 31), time(19, 31))
    # dry_at crept +24 min (> REWRITE_MARGIN) → extend end, start still pinned
    assert gate.evaluate(_dt(16, 50), TARGET, _dt(19, 55), True) is True
    assert gate.override == NonWorkHours(time(16, 31), time(19, 55))


def test_release_clears() -> None:
    gate = RainGate(override=NonWorkHours(time(16, 31), time(19, 31)))
    assert gate.release() is True
    assert gate.override is None


# --- RainGateService ---


def _service(
    *,
    target: NonWorkHours | None = TARGET,
    dry_at: datetime | None = None,
    docked: bool = True,
    progress: int = 50,
    now: datetime = WORK,
) -> tuple[RainGateService, MagicMock, MagicMock]:
    non_work = MagicMock()
    non_work.effective_hours = target
    rain = MagicMock()
    rain.dry_at = dry_at
    luba = MagicMock()
    luba.read_at_dock.return_value = docked
    luba.read_progress.return_value = progress
    actuator = MagicMock()
    actuator.apply = MagicMock(return_value="coro")  # not awaited — handed to tasks
    tasks = MagicMock()
    service = RainGateService(non_work, rain, actuator, luba, tasks, lambda: now)
    return service, actuator, tasks


def test_service_no_target_does_nothing() -> None:
    service, actuator, tasks = _service(target=None)
    service.evaluate()
    tasks.run_background.assert_not_called()
    actuator.apply.assert_not_called()


def test_service_blocks_when_docked_with_task() -> None:
    service, actuator, tasks = _service(dry_at=_dt(19, 31), docked=True, progress=50)
    notified: list[int] = []
    service.add_listener(lambda: notified.append(1))

    service.evaluate()

    assert service.override == NonWorkHours(time(16, 31), time(19, 31))
    actuator.apply.assert_called_once_with(NonWorkHours(time(16, 31), time(19, 31)))
    tasks.run_background.assert_called_once()
    assert notified == [1]


def test_service_no_block_when_not_docked() -> None:
    service, actuator, tasks = _service(dry_at=_dt(19, 31), docked=False)
    service.evaluate()
    assert service.override is None
    tasks.run_background.assert_not_called()


def test_service_no_block_when_no_task() -> None:
    service, actuator, tasks = _service(dry_at=_dt(19, 31), docked=True, progress=0)
    service.evaluate()
    assert service.override is None
    tasks.run_background.assert_not_called()


def test_service_extends_at_morning_boundary() -> None:
    service, actuator, tasks = _service(dry_at=_dt(12, 0), now=NEAR, docked=False)
    service.evaluate()
    assert service.override == NonWorkHours(time(20, 35), time(12, 0))
    actuator.apply.assert_called_once_with(NonWorkHours(time(20, 35), time(12, 0)))


def test_service_restore_pushes_target_when_dry() -> None:
    service, actuator, tasks = _service(dry_at=None)
    service._gate.override = NonWorkHours(time(16, 31), time(19, 31))  # noqa: SLF001

    service.evaluate()

    assert service.override is None
    actuator.apply.assert_called_once_with(TARGET)
    tasks.run_background.assert_called_once()


def test_clear_hold_releases_and_restores_target() -> None:
    service, actuator, tasks = _service()
    service._gate.override = NonWorkHours(time(16, 31), time(19, 31))  # noqa: SLF001
    notified: list[int] = []
    service.add_listener(lambda: notified.append(1))

    service.clear_hold()

    assert service.override is None
    actuator.apply.assert_called_once_with(TARGET)
    assert notified == [1]


def test_clear_hold_noop_when_not_holding() -> None:
    service, actuator, tasks = _service()
    service.clear_hold()
    actuator.apply.assert_not_called()
    tasks.run_background.assert_not_called()
