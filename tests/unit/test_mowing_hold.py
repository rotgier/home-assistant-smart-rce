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
    assert hold.evaluate(NEAR, None, None, False).override_changed is False
    assert hold.override is None


def test_dry_clears() -> None:
    hold = MowingHold(override=NonWorkHours(time(16, 16), time(19, 31)))
    result = hold.evaluate(_dt(20, 10), TARGET, _dt(19, 55), True)  # dry_at past
    assert result.override_changed is True
    assert hold.override is None


def test_working_hours_docked_with_task_holds_until_dry_at() -> None:
    hold = MowingHold()
    assert hold.evaluate(WORK, TARGET, _dt(19, 31), True).override_changed is True
    assert hold.override == NonWorkHours(time(16, 16), time(19, 31))  # start = now-15


def test_hold_truncates_override_to_whole_minutes() -> None:
    # now + dry_at carry seconds/micros; override must be minute-clean so it
    # equals the device's minute-resolution report (else phantom drift_*).
    hold = MowingHold()
    now = datetime(2026, 6, 13, 16, 31, 23, 456789, tzinfo=UTC)
    dry_at = datetime(2026, 6, 13, 19, 31, 45, 999, tzinfo=UTC)
    assert hold.evaluate(now, TARGET, dry_at, True, force=True).override_changed is True
    assert hold.override == NonWorkHours(time(16, 16), time(19, 31))
    assert hold.override is not None
    assert (hold.override.start.second, hold.override.start.microsecond) == (0, 0)
    assert (hold.override.end.second, hold.override.end.microsecond) == (0, 0)


def test_working_hours_not_docked_does_not_hold() -> None:
    hold = MowingHold()
    assert hold.evaluate(WORK, TARGET, _dt(19, 31), False).override_changed is False
    assert hold.override is None


def test_hold_skips_while_ahead_refreshes_near_expiry() -> None:
    hold = MowingHold()
    assert (
        hold.evaluate(_dt(16, 31), TARGET, _dt(19, 31), True).override_changed is True
    )
    assert hold.override == NonWorkHours(time(16, 16), time(19, 31))
    # end (19:31) still far ahead → skip despite dry_at creep, start pinned
    assert hold.evaluate(_dt(17, 0), TARGET, _dt(20, 0), True).override_changed is False
    assert hold.override == NonWorkHours(time(16, 16), time(19, 31))
    # within MARGIN of the end + still wet → refresh end to current dry_at
    assert (
        hold.evaluate(_dt(19, 20), TARGET, _dt(22, 20), True).override_changed is True
    )
    assert hold.override == NonWorkHours(time(16, 16), time(22, 20))


def test_near_morning_wet_past_end_holds() -> None:
    hold = MowingHold()
    # dry_at 12:00 > 10:05
    assert hold.evaluate(NEAR, TARGET, _dt(12, 0), True).override_changed is True
    assert hold.override == NonWorkHours(time(9, 43), time(12, 0))  # start = now-15


def test_near_morning_dry_by_end_does_not_hold() -> None:
    hold = MowingHold()
    # dry_at ≤ 10:05
    assert hold.evaluate(NEAR, TARGET, _dt(10, 0), True).override_changed is False
    assert hold.override is None


def test_hold_continues_past_morning_end_without_rewrite() -> None:
    hold = MowingHold(override=NonWorkHours(time(9, 43), time(12, 0)))
    assert hold.evaluate(_dt(10, 6), TARGET, _dt(12, 0), True).override_changed is False
    assert hold.override == NonWorkHours(time(9, 43), time(12, 0))


def test_deep_in_quiet_drops_the_hold() -> None:
    hold = MowingHold(override=NonWorkHours(time(16, 16), time(23, 0)))
    assert hold.evaluate(_dt(3, 0), TARGET, _dt(5, 0), True).override_changed is True
    assert hold.override is None


def test_evening_start_buffer_keeps_hold() -> None:
    hold = MowingHold(override=NonWorkHours(time(16, 16), time(23, 0)))
    # 5 min in
    assert (
        hold.evaluate(_dt(20, 40), TARGET, _dt(23, 0), True).override_changed is False
    )
    assert hold.override == NonWorkHours(time(16, 16), time(23, 0))
    # MARGIN past
    assert hold.evaluate(_dt(20, 50), TARGET, _dt(23, 0), True).override_changed is True
    assert hold.override is None


# --- MowingHold domain: rain suppression (clear button) ---


def test_suppress_rain_then_evaluate_clears() -> None:
    hold = MowingHold(override=NonWorkHours(time(16, 16), time(19, 31)))
    hold.suppress_rain(WORK)  # suppress rain until 16:51
    # clears
    assert hold.evaluate(WORK, TARGET, _dt(19, 31), True, force=True).override_changed
    assert hold.override is None
    # tick 5 min later — still docked + wet — stays released (suppressed)
    assert (
        hold.evaluate(_dt(16, 36), TARGET, _dt(19, 31), True).override_changed is False
    )
    assert hold.override is None


def test_rehold_after_grace_if_still_docked_and_wet() -> None:
    hold = MowingHold()
    hold.suppress_rain(WORK)  # suppress until 16:51
    # suppressed
    assert (
        hold.evaluate(_dt(16, 40), TARGET, _dt(19, 31), True).override_changed is False
    )
    # past grace
    assert (
        hold.evaluate(_dt(16, 52), TARGET, _dt(19, 31), True).override_changed is True
    )
    assert hold.override == NonWorkHours(time(16, 37), time(19, 31))  # start = now-15


# --- MowingHold domain: manual park ---


def test_manual_park_holds_regardless_of_dock_and_rain() -> None:
    hold = MowingHold()
    assert hold.set_manual(WORK, 30) is True
    # not docked, dry → still holds by manual, until WORK+30 (17:01)
    assert hold.evaluate(WORK, TARGET, None, False, force=True).override_changed is True
    assert hold.override == NonWorkHours(time(16, 16), time(17, 1))


def test_effective_end_is_max_of_rain_and_manual() -> None:
    hold = MowingHold()
    hold.set_manual(WORK, 30)  # manual until 17:01
    # rain dry_at 19:31 is later than manual 17:01 → end = 19:31
    assert hold.evaluate(WORK, TARGET, _dt(19, 31), True, force=True).override_changed
    assert hold.override == NonWorkHours(time(16, 16), time(19, 31))


def test_manual_survives_after_rain_clears() -> None:
    hold = MowingHold()
    hold.set_manual(WORK, 30)  # until 17:01
    hold.suppress_rain(WORK)  # rain suppressed
    # dry + suppressed, but manual keeps it held until 17:01
    assert hold.evaluate(WORK, TARGET, None, True, force=True).override_changed is True
    assert hold.override == NonWorkHours(time(16, 16), time(17, 1))


def test_evaluate_expires_manual_and_reports_cleared() -> None:
    hold = MowingHold()
    hold.set_manual(WORK, 30)  # until 17:01
    # before expiry: still counted, nothing cleared
    before = hold.evaluate(WORK, TARGET, None, False, force=True)
    assert before.manual_cleared is False
    assert hold.manual_until == WORK + timedelta(minutes=30)

    # past 17:01: evaluate clears the lapsed deadline (no lingering state)
    after = hold.evaluate(_dt(17, 5), TARGET, None, False, force=True)
    assert after.manual_cleared is True
    assert after.override_changed is True  # sole hold dropped too
    assert hold.manual_until is None
    assert hold.manual_since is None
    assert hold.override is None


def test_manual_expiry_no_double_clear_on_next_tick() -> None:
    hold = MowingHold()
    hold.set_manual(WORK, 30)
    hold.evaluate(_dt(17, 5), TARGET, None, False, force=True)  # clears (True)
    # already cleared → a later tick reports nothing new
    assert hold.evaluate(_dt(17, 10), TARGET, None, False).manual_cleared is False


def test_cancel_manual_releases_when_dry() -> None:
    hold = MowingHold()
    hold.set_manual(WORK, 30)
    hold.evaluate(WORK, TARGET, None, False, force=True)  # held by manual
    assert hold.cancel_manual() is True
    # releases
    assert hold.evaluate(WORK, TARGET, None, False, force=True).override_changed is True
    assert hold.override is None


def test_cancel_manual_keeps_rain_hold() -> None:
    hold = MowingHold()
    hold.set_manual(WORK, 30)
    hold.evaluate(WORK, TARGET, _dt(19, 31), True, force=True)  # held (max=19:31)
    hold.cancel_manual()
    # rain still active → stays held (now anchored on dry_at)
    result = hold.evaluate(WORK, TARGET, _dt(19, 31), True, force=True)
    assert result.override_changed is False
    assert hold.override == NonWorkHours(time(16, 16), time(19, 31))


# --- MowingHold domain: evening-buffer bump (TARGET quiet start 20:35) ---


def test_evening_buffer_bumps_manual_end_past_the_buffer() -> None:
    # Manual park ending inside [quiet_start, quiet_start+MARGIN] (20:35-20:50)
    # must push the override end PAST the buffer — else it expires mid-buffer and
    # the restore skip keeps the lapsed override, freeing the mower (2026-07-26
    # park to 20:38 → mowed 20:39).
    hold = MowingHold()
    hold.set_manual(_dt(20, 25), 15)  # manual until 20:40 — inside the buffer
    result = hold.evaluate(_dt(20, 36), TARGET, None, False, force=True)
    assert result.override_changed is True
    # end 20:40 → quiet_start 20:35 + MARGIN 15 + BUFFER_CLEAR 5 = 20:55
    assert hold.override == NonWorkHours(time(20, 21), time(20, 55))  # start = 20:36-15


def test_evening_buffer_bumps_rain_end_past_the_buffer() -> None:
    # Same bump for a RAIN hold — the rule lives on the combined max in
    # `_desired_end`, so manual and rain are both covered by one place.
    hold = MowingHold()
    # work hours 20:20, docked-with-task, rain dry_at 20:44 (inside the buffer)
    result = hold.evaluate(_dt(20, 20), TARGET, _dt(20, 44), True, force=True)
    assert result.override_changed is True
    assert hold.override == NonWorkHours(time(20, 5), time(20, 55))  # 20:44 → 20:55


def test_evening_buffer_no_bump_before_quiet_start() -> None:
    # Park ending BEFORE the quiet start (20:34 < 20:35) is outside the window —
    # she is legitimately free that last minute; no bump.
    hold = MowingHold()
    hold.set_manual(_dt(20, 19), 15)  # manual until 20:34
    hold.evaluate(_dt(20, 30), TARGET, None, False, force=True)
    assert hold.override == NonWorkHours(time(20, 15), time(20, 34))  # end NOT bumped


def test_evening_buffer_hold_survives_manual_expiry_until_release() -> None:
    # The point: the bumped override COVERS the buffer window, so after the
    # manual really lapses the restore skip keeps a still-covering window (mower
    # stays parked), then hands to the target when the buffer releases — no gap.
    hold = MowingHold()
    hold.set_manual(_dt(20, 25), 15)  # until 20:40 → override end bumped to 20:55
    hold.evaluate(_dt(20, 36), TARGET, None, False, force=True)
    assert hold.override == NonWorkHours(time(20, 21), time(20, 55))
    # 20:41: manual lapsed, but inside the buffer AND override still covers now
    # (20:41 < 20:55) → kept → mower stays parked
    assert hold.evaluate(_dt(20, 41), TARGET, None, False).override_changed is False
    assert hold.override == NonWorkHours(time(20, 21), time(20, 55))
    # 20:51: >MARGIN past quiet start 20:35 → buffer releases → restore target
    assert hold.evaluate(_dt(20, 51), TARGET, None, False).override_changed is True
    assert hold.override is None


def test_manual_park_round_trips_through_to_dict() -> None:
    hold = MowingHold()
    hold.set_manual(WORK, 30)
    assert hold.manual_since == WORK  # armed-at stamped
    restored = MowingHold.from_dict(hold.to_dict())
    assert restored.manual_until == WORK + timedelta(minutes=30)
    assert restored.manual_since == WORK


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
    service_mode: bool = False,
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
    svc_mode = MagicMock()
    svc_mode.is_active = service_mode
    service = MowingHoldService(
        repo, non_work, rain, actuator, luba, tasks, svc_mode, lambda: now
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


def test_service_cancel_park_notifies_when_override_unchanged() -> None:
    # Manual park while rain holds LATER (dry_at 19:31 > manual 17:01): the
    # override is anchored on rain, so cancelling the manual does NOT change the
    # override — but the manual window on the sensor must still refresh. Bug: the
    # old code only notified on an override change → stale manual window on screen.
    service, actuator, tasks = _service(dry_at=_dt(19, 31), docked=True, now=WORK)
    service.park(30)  # manual 17:01; override anchored on rain 19:31
    notified: list[int] = []
    service.add_listener(lambda: notified.append(1))

    service.cancel_park()

    assert service.is_manual_parked is False
    assert service.override == NonWorkHours(
        time(16, 16), time(19, 31)
    )  # rain, unchanged
    assert notified == [1]  # refreshed despite the override not moving


def test_service_tick_expiry_persists_and_notifies() -> None:
    # A minutely tick past the manual deadline must reset + persist it and wake
    # the sensor — no user action, no override change (dry, not docked).
    service, actuator, tasks = _service(dry_at=None, docked=False, now=WORK)
    service.park(30)  # manual until 17:01
    service._repo.save_if_changed.reset_mock()  # noqa: SLF001
    service._now = lambda: _dt(17, 5)  # noqa: SLF001 — tick past expiry
    notified: list[int] = []
    service.add_listener(lambda: notified.append(1))

    service.evaluate()  # tick

    assert service.is_manual_parked is False
    assert service.manual_until is None
    service._repo.save_if_changed.assert_called_once()  # noqa: SLF001 — clear persisted
    assert notified == [1]


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


def test_service_mode_skips_push() -> None:
    # Maintenance mode → hold does not touch the device (no non-work push), even
    # when a hold would otherwise apply (docked-with-task + wet grass).
    service, actuator, tasks = _service(
        dry_at=_dt(19, 31), docked=True, progress=50, service_mode=True
    )

    service.evaluate()

    assert service.override is None
    actuator.apply.assert_not_called()
    tasks.run_background.assert_not_called()
