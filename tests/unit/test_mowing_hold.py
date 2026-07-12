"""Unit tests for the mowing hold — MowingHold domain + MowingHoldService logic."""

from datetime import UTC, datetime, time, timedelta
from unittest.mock import MagicMock

from custom_components.smart_rce.garden.application.mowing_hold_service import (
    MowingHoldService,
)
from custom_components.smart_rce.garden.domain.mowing_hold import MowingHold
from custom_components.smart_rce.garden.domain.non_work import NonWorkHours

TARGET = NonWorkHours(time(20, 35), time(10, 5))  # quiet 20:35 → 10:05
NEAR = datetime(2026, 6, 13, 9, 58, tzinfo=UTC)  # inside quiet, ≤MARGIN to 10:05 end
WORK = datetime(2026, 6, 13, 16, 31, tzinfo=UTC)  # working hours (10:05–20:35)


def _dt(hour: int, minute: int) -> datetime:
    return datetime(2026, 6, 13, hour, minute, tzinfo=UTC)


# --- MowingHold domain: rain hold ---


def test_idle_when_no_target() -> None:
    hold = MowingHold()
    assert hold.evaluate(NEAR, None, None, False) is False
    assert hold.override is None


def test_dry_clears() -> None:
    hold = MowingHold(override=NonWorkHours(time(16, 16), time(19, 31)))
    assert hold.evaluate(_dt(20, 10), TARGET, _dt(19, 55), True) is True  # dry_at past
    assert hold.override is None


def test_working_hours_docked_with_task_holds_until_dry_at() -> None:
    hold = MowingHold()
    assert hold.evaluate(WORK, TARGET, _dt(19, 31), True) is True
    assert hold.override == NonWorkHours(time(16, 16), time(19, 31))  # start = now-15


def test_working_hours_not_docked_does_not_hold() -> None:
    hold = MowingHold()
    assert hold.evaluate(WORK, TARGET, _dt(19, 31), False) is False
    assert hold.override is None


def test_hold_skips_while_ahead_refreshes_near_expiry() -> None:
    hold = MowingHold()
    assert hold.evaluate(_dt(16, 31), TARGET, _dt(19, 31), True) is True
    assert hold.override == NonWorkHours(time(16, 16), time(19, 31))
    # end (19:31) still far ahead → skip despite dry_at creep, start pinned
    assert hold.evaluate(_dt(17, 0), TARGET, _dt(20, 0), True) is False
    assert hold.override == NonWorkHours(time(16, 16), time(19, 31))
    # within MARGIN of the end + still wet → refresh end to current dry_at
    assert hold.evaluate(_dt(19, 20), TARGET, _dt(22, 20), True) is True
    assert hold.override == NonWorkHours(time(16, 16), time(22, 20))


def test_near_morning_wet_past_end_holds() -> None:
    hold = MowingHold()
    assert hold.evaluate(NEAR, TARGET, _dt(12, 0), True) is True  # dry_at 12:00 > 10:05
    assert hold.override == NonWorkHours(time(9, 43), time(12, 0))  # start = now-15


def test_near_morning_dry_by_end_does_not_hold() -> None:
    hold = MowingHold()
    assert hold.evaluate(NEAR, TARGET, _dt(10, 0), True) is False  # dry_at ≤ 10:05
    assert hold.override is None


def test_hold_continues_past_morning_end_without_rewrite() -> None:
    hold = MowingHold(override=NonWorkHours(time(9, 43), time(12, 0)))
    assert hold.evaluate(_dt(10, 6), TARGET, _dt(12, 0), True) is False
    assert hold.override == NonWorkHours(time(9, 43), time(12, 0))


def test_deep_in_quiet_drops_the_hold() -> None:
    hold = MowingHold(override=NonWorkHours(time(16, 16), time(23, 0)))
    assert hold.evaluate(_dt(3, 0), TARGET, _dt(5, 0), True) is True
    assert hold.override is None


def test_evening_start_buffer_keeps_hold() -> None:
    hold = MowingHold(override=NonWorkHours(time(16, 16), time(23, 0)))
    assert hold.evaluate(_dt(20, 40), TARGET, _dt(23, 0), True) is False  # 5 min in
    assert hold.override == NonWorkHours(time(16, 16), time(23, 0))
    assert hold.evaluate(_dt(20, 50), TARGET, _dt(23, 0), True) is True  # MARGIN past
    assert hold.override is None


# --- MowingHold domain: rain suppression (clear button) ---


def test_suppress_rain_then_evaluate_clears() -> None:
    hold = MowingHold(override=NonWorkHours(time(16, 16), time(19, 31)))
    hold.suppress_rain(WORK)  # suppress rain until 16:51
    assert hold.evaluate(WORK, TARGET, _dt(19, 31), True, force=True) is True  # clears
    assert hold.override is None
    # tick 5 min later — still docked + wet — stays released (suppressed)
    assert hold.evaluate(_dt(16, 36), TARGET, _dt(19, 31), True) is False
    assert hold.override is None


def test_rehold_after_grace_if_still_docked_and_wet() -> None:
    hold = MowingHold()
    hold.suppress_rain(WORK)  # suppress until 16:51
    assert hold.evaluate(_dt(16, 40), TARGET, _dt(19, 31), True) is False  # suppressed
    assert hold.evaluate(_dt(16, 52), TARGET, _dt(19, 31), True) is True  # past grace
    assert hold.override == NonWorkHours(time(16, 37), time(19, 31))  # start = now-15


# --- MowingHold domain: manual park ---


def test_manual_park_holds_regardless_of_dock_and_rain() -> None:
    hold = MowingHold()
    assert hold.set_manual(WORK, 30) is True
    # not docked, dry → still holds by manual, until WORK+30 (17:01)
    assert hold.evaluate(WORK, TARGET, None, False, force=True) is True
    assert hold.override == NonWorkHours(time(16, 16), time(17, 1))


def test_effective_end_is_max_of_rain_and_manual() -> None:
    hold = MowingHold()
    hold.set_manual(WORK, 30)  # manual until 17:01
    # rain dry_at 19:31 is later than manual 17:01 → end = 19:31
    assert hold.evaluate(WORK, TARGET, _dt(19, 31), True, force=True) is True
    assert hold.override == NonWorkHours(time(16, 16), time(19, 31))


def test_manual_survives_after_rain_clears() -> None:
    hold = MowingHold()
    hold.set_manual(WORK, 30)  # until 17:01
    hold.suppress_rain(WORK)  # rain suppressed
    # dry + suppressed, but manual keeps it held until 17:01
    assert hold.evaluate(WORK, TARGET, None, True, force=True) is True
    assert hold.override == NonWorkHours(time(16, 16), time(17, 1))


def test_manual_expiry_releases_when_dry() -> None:
    hold = MowingHold()
    hold.set_manual(WORK, 30)  # until 17:01
    hold.evaluate(WORK, TARGET, None, False, force=True)  # held by manual
    # past 17:01, dry → nothing active → release
    assert hold.evaluate(_dt(17, 5), TARGET, None, False, force=True) is True
    assert hold.override is None


def test_cancel_manual_releases_when_dry() -> None:
    hold = MowingHold()
    hold.set_manual(WORK, 30)
    hold.evaluate(WORK, TARGET, None, False, force=True)  # held by manual
    assert hold.cancel_manual() is True
    assert hold.evaluate(WORK, TARGET, None, False, force=True) is True  # releases
    assert hold.override is None


def test_cancel_manual_keeps_rain_hold() -> None:
    hold = MowingHold()
    hold.set_manual(WORK, 30)
    hold.evaluate(WORK, TARGET, _dt(19, 31), True, force=True)  # held (max=19:31)
    hold.cancel_manual()
    # rain still active → stays held (now anchored on dry_at)
    assert hold.evaluate(WORK, TARGET, _dt(19, 31), True, force=True) is False
    assert hold.override == NonWorkHours(time(16, 16), time(19, 31))


def test_manual_until_round_trips_through_to_dict() -> None:
    hold = MowingHold()
    hold.set_manual(WORK, 30)
    restored = MowingHold.from_dict(hold.to_dict())
    assert restored.manual_until == WORK + timedelta(minutes=30)


def test_from_dict_empty_no_manual() -> None:
    assert MowingHold.from_dict({}).manual_until is None


# --- MowingHoldService ---


def _service(
    *,
    target: NonWorkHours | None = TARGET,
    dry_at: datetime | None = None,
    docked: bool = True,
    progress: int = 50,
    now: datetime = WORK,
) -> tuple[MowingHoldService, MagicMock, MagicMock]:
    repo = MagicMock()
    repo.state = MowingHold()
    repo.save_if_changed = MagicMock()
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
    service = MowingHoldService(
        repo, non_work, rain, actuator, luba, tasks, lambda: now
    )
    return service, actuator, tasks


def test_service_no_target_does_nothing() -> None:
    service, actuator, tasks = _service(target=None)
    service.evaluate()
    tasks.run_background.assert_not_called()
    actuator.apply.assert_not_called()


def test_service_holds_when_docked_with_task() -> None:
    service, actuator, tasks = _service(dry_at=_dt(19, 31), docked=True, progress=50)
    notified: list[int] = []
    service.add_listener(lambda: notified.append(1))

    service.evaluate()

    assert service.override == NonWorkHours(time(16, 16), time(19, 31))  # now-15
    actuator.apply.assert_called_once_with(NonWorkHours(time(16, 16), time(19, 31)))
    tasks.run_background.assert_called_once()
    assert notified == [1]


def test_service_no_hold_when_not_docked() -> None:
    service, actuator, tasks = _service(dry_at=_dt(19, 31), docked=False)
    service.evaluate()
    assert service.override is None
    tasks.run_background.assert_not_called()


def test_service_restore_pushes_target_when_dry() -> None:
    service, actuator, tasks = _service(dry_at=None)
    service._hold.override = NonWorkHours(time(16, 16), time(19, 31))  # noqa: SLF001

    service.evaluate()

    assert service.override is None
    actuator.apply.assert_called_once_with(TARGET)


def test_service_park_holds_and_persists() -> None:
    service, actuator, tasks = _service(dry_at=None, docked=False, now=WORK)

    service.park(30)

    assert service.is_manual_parked is True
    assert service.override == NonWorkHours(time(16, 16), time(17, 1))
    actuator.apply.assert_called_once_with(NonWorkHours(time(16, 16), time(17, 1)))
    service._repo.save_if_changed.assert_called_once()  # noqa: SLF001


def test_service_cancel_park_restores_target_when_dry() -> None:
    service, actuator, tasks = _service(dry_at=None, docked=False, now=WORK)
    service.park(30)
    actuator.apply.reset_mock()

    service.cancel_park()

    assert service.is_manual_parked is False
    assert service.override is None
    actuator.apply.assert_called_once_with(TARGET)


def test_service_clear_hold_keeps_manual_park() -> None:
    service, actuator, tasks = _service(dry_at=_dt(19, 31), docked=True, now=WORK)
    service.park(30)  # manual until 17:01
    actuator.apply.reset_mock()

    service.clear_hold()  # suppress rain — manual must survive

    assert service.is_manual_parked is True
    assert service.override == NonWorkHours(time(16, 16), time(17, 1))


def test_clear_hold_noop_when_not_holding() -> None:
    service, actuator, tasks = _service(dry_at=None, docked=False)
    service.clear_hold()
    actuator.apply.assert_not_called()
    tasks.run_background.assert_not_called()
