"""Unit tests for the rain gate — RainGate domain + RainGateService push logic."""

from datetime import UTC, datetime, time
from unittest.mock import MagicMock

from custom_components.smart_rce.garden.application.rain_gate_service import (
    RainGateService,
)
from custom_components.smart_rce.garden.domain.non_work import NonWorkHours
from custom_components.smart_rce.garden.domain.rain_gate import RainGate

TARGET = NonWorkHours(time(20, 35), time(10, 5))  # quiet 20:35 → 10:05
NEAR = datetime(2026, 6, 13, 9, 58, tzinfo=UTC)  # inside quiet, ≤MARGIN to 10:05 end
WORK = datetime(2026, 6, 13, 16, 31, tzinfo=UTC)  # working hours (10:05–20:35)


def _dt(hour: int, minute: int) -> datetime:
    return datetime(2026, 6, 13, hour, minute, tzinfo=UTC)


# --- RainGate domain ---


def test_idle_when_no_target() -> None:
    gate = RainGate()
    assert gate.evaluate(NEAR, None, None, False) is False
    assert gate.override is None


def test_dry_clears() -> None:
    gate = RainGate(override=NonWorkHours(time(16, 16), time(19, 31)))
    assert gate.evaluate(_dt(20, 10), TARGET, _dt(19, 55), True) is True  # dry_at past
    assert gate.override is None


# working hours — mid-day charge-resume block


def test_working_hours_docked_with_task_blocks_until_dry_at() -> None:
    gate = RainGate()
    assert gate.evaluate(WORK, TARGET, _dt(19, 31), True) is True
    assert gate.override == NonWorkHours(time(16, 16), time(19, 31))  # start = now-15


def test_working_hours_not_docked_does_not_block() -> None:
    gate = RainGate()
    assert gate.evaluate(WORK, TARGET, _dt(19, 31), False) is False
    assert gate.override is None


def test_block_skips_while_ahead_refreshes_near_expiry() -> None:
    gate = RainGate()
    assert gate.evaluate(_dt(16, 31), TARGET, _dt(19, 31), True) is True
    assert gate.override == NonWorkHours(time(16, 16), time(19, 31))
    # end (19:31) still far ahead → skip despite dry_at creep, start pinned
    assert gate.evaluate(_dt(17, 0), TARGET, _dt(20, 0), True) is False
    assert gate.override == NonWorkHours(time(16, 16), time(19, 31))
    # within MARGIN of the end + still wet → refresh end to current dry_at
    assert gate.evaluate(_dt(19, 20), TARGET, _dt(22, 20), True) is True
    assert gate.override == NonWorkHours(time(16, 16), time(22, 20))


# morning quiet-end — same block, only when still wet past the end


def test_near_morning_wet_past_end_blocks() -> None:
    gate = RainGate()
    assert gate.evaluate(NEAR, TARGET, _dt(12, 0), True) is True  # dry_at 12:00 > 10:05
    assert gate.override == NonWorkHours(time(9, 43), time(12, 0))  # start = now-15


def test_near_morning_dry_by_end_does_not_block() -> None:
    gate = RainGate()
    assert (
        gate.evaluate(NEAR, TARGET, _dt(10, 0), True) is False
    )  # dry_at 10:00 ≤ 10:05
    assert gate.override is None


def test_block_continues_past_morning_end_without_rewrite() -> None:
    # A near-morning block; the clock passes the user end (10:05) → working hours,
    # but the block just keeps holding (start already in the past) — no rewrite,
    # no gap.
    gate = RainGate(override=NonWorkHours(time(9, 43), time(12, 0)))
    assert gate.evaluate(_dt(10, 6), TARGET, _dt(12, 0), True) is False
    assert gate.override == NonWorkHours(time(9, 43), time(12, 0))


def test_deep_in_quiet_drops_the_block() -> None:
    # Deep in the night quiet window — the real non-work parks the mower, so the
    # override is dropped (restore target), even though it is still wet.
    gate = RainGate(override=NonWorkHours(time(16, 16), time(23, 0)))
    assert gate.evaluate(_dt(3, 0), TARGET, _dt(5, 0), True) is True
    assert gate.override is None


def test_evening_start_buffer_keeps_block() -> None:
    # Just past the evening start (20:35) the real window would park, but flipping
    # to target could race a lagging device clock, so the held block stays.
    gate = RainGate(override=NonWorkHours(time(16, 16), time(23, 0)))
    assert gate.evaluate(_dt(20, 40), TARGET, _dt(23, 0), True) is False  # 5 min in
    assert gate.override == NonWorkHours(time(16, 16), time(23, 0))
    # MARGIN past the start → safe to restore the target
    assert gate.evaluate(_dt(20, 50), TARGET, _dt(23, 0), True) is True
    assert gate.override is None


def test_release_clears() -> None:
    gate = RainGate(override=NonWorkHours(time(16, 16), time(19, 31)))
    assert gate.release(WORK) is True
    assert gate.override is None


def test_release_suppresses_reblock_while_still_docked_and_wet() -> None:
    # Clicking clear while the mower is still on the dock + wet: the next tick
    # must NOT re-block (it needs time to undock; cloud lags the state read).
    gate = RainGate(override=NonWorkHours(time(16, 16), time(19, 31)))
    gate.release(WORK)  # 16:31
    assert gate.override is None
    # A tick 5 min later — still docked + wet — stays released (suppressed).
    assert gate.evaluate(_dt(16, 36), TARGET, _dt(19, 31), True) is False
    assert gate.override is None


def test_reblock_after_grace_if_still_docked_and_wet() -> None:
    gate = RainGate(override=NonWorkHours(time(16, 16), time(19, 31)))
    gate.release(WORK)  # suppress until 16:51
    assert gate.evaluate(_dt(16, 40), TARGET, _dt(19, 31), True) is False  # suppressed
    # Past the 20-min grace, still docked + wet → re-asserts the block.
    assert gate.evaluate(_dt(16, 52), TARGET, _dt(19, 31), True) is True
    assert gate.override == NonWorkHours(time(16, 37), time(19, 31))  # start = now-15


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

    assert service.override == NonWorkHours(time(16, 16), time(19, 31))  # now-15
    actuator.apply.assert_called_once_with(NonWorkHours(time(16, 16), time(19, 31)))
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


def test_service_blocks_near_morning() -> None:
    service, actuator, tasks = _service(dry_at=_dt(12, 0), now=NEAR)
    service.evaluate()
    assert service.override == NonWorkHours(time(9, 43), time(12, 0))
    actuator.apply.assert_called_once_with(NonWorkHours(time(9, 43), time(12, 0)))


def test_service_restore_pushes_target_when_dry() -> None:
    service, actuator, tasks = _service(dry_at=None)
    service._gate.override = NonWorkHours(time(16, 16), time(19, 31))  # noqa: SLF001

    service.evaluate()

    assert service.override is None
    actuator.apply.assert_called_once_with(TARGET)
    tasks.run_background.assert_called_once()


def test_clear_hold_releases_and_restores_target() -> None:
    service, actuator, tasks = _service()
    service._gate.override = NonWorkHours(time(16, 16), time(19, 31))  # noqa: SLF001
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
