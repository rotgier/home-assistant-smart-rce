"""Slot sessions and upcoming runs — the dashboard's read models."""

from datetime import date, datetime, timedelta, timezone

from custom_components.smart_rce.domain.battery_schedule import (
    BatterySchedule,
    BatteryScheduleEntry,
    DisengageReason,
    MorningDischargePlan,
    ReserveWindow,
    SlotKind,
    SlotSession,
    T1SocReserve,
)
import pytest

TZ = timezone.utc
DAY = date(2026, 10, 15)  # Thursday


def _at(h: int, m: int = 0, s: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=TZ)


def _schedule() -> BatterySchedule:
    """Both auto slots on, reserve 37 for the night, plan 08-09 down to 22."""
    today = BatteryScheduleEntry.defaults_for_all_kinds()
    for kind in (SlotKind.CHARGE_MORNING_AUTO, SlotKind.DISCHARGE_MORNING_AUTO):
        today[kind] = BatteryScheduleEntry.default_for(kind, enabled=True)
    sch = BatterySchedule(today=today)
    sch.set_t1_reserve(
        T1SocReserve(
            day=DAY,
            value=37,
            flat=37,
            prev_without_worst=None,
            dip_kwh=None,
            source="live_soft",
            window=ReserveWindow.FULL,
        ),
        _at(1),
    )
    sch.set_morning_discharge(
        MorningDischargePlan.decide(
            day=DAY, hour=8, gross_price=1000.0, reserve=22, fill=20
        )
    )
    return sch


def _run_until_idle(
    sch: BatterySchedule, start: datetime, soc: float, step: float
) -> tuple[datetime, float]:
    """Tick every minute from `start`, moving SoC by `step` while engaged."""
    now = start
    sch.compute_operation(now, soc, is_workday=True)
    while sch.currently_engaging is not None:
        now += timedelta(minutes=1)
        soc += step
        sch.compute_operation(now, soc, is_workday=True)
    return now, soc


class TestSessions:
    def test_engage_opens_and_disengage_closes_at_the_level_reached(self):
        sch = _schedule()
        sch.compute_operation(_at(6, 40), 20.0, is_workday=True)
        [open_session] = sch.sessions_on(DAY)
        assert open_session.is_open
        assert (open_session.slot, open_session.start_soc) == (
            SlotKind.CHARGE_MORNING_AUTO,
            20.0,
        )
        assert open_session.target_soc == 37

        sch.compute_operation(_at(6, 52), 38.0, is_workday=True)
        [closed] = sch.sessions_on(DAY)
        assert closed.ended_at == _at(6, 52)
        assert closed.end_soc == 38.0
        assert closed.reason is DisengageReason.TARGET_REACHED

    def test_window_end_is_recorded_as_such(self):
        sch = _schedule()
        sch.compute_operation(_at(8, 40), 37.0, is_workday=True)
        sch.compute_operation(_at(9, 0), 25.0, is_workday=True)
        [session] = sch.sessions_on(DAY)
        assert (session.end_soc, session.reason) == (
            25.0,
            DisengageReason.WINDOW_ENDED,
        )

    def test_open_session_reports_the_target_in_force_now(self):
        sch = _schedule()
        sch.compute_operation(_at(8, 40), 37.0, is_workday=True)
        sch.set_morning_discharge(
            MorningDischargePlan.decide(
                day=DAY, hour=8, gross_price=1000.0, reserve=24, fill=20
            )
        )
        [session] = sch.sessions_on(DAY)
        assert session.target_soc == 24

    def test_restart_mid_session_keeps_it_open_and_closes_it_later(self):
        sch = _schedule()
        sch.compute_operation(_at(6, 40), 20.0, is_workday=True)

        restored = BatterySchedule.from_dict(sch.to_dict())
        restored.set_t1_reserve(sch.t1_reserve, _at(6, 41))
        assert restored.currently_engaging is SlotKind.CHARGE_MORNING_AUTO
        [session] = restored.sessions_on(DAY)
        assert session.is_open and session.started_at == _at(6, 40)

        restored.compute_operation(_at(6, 52), 37.0, is_workday=True)
        [session] = restored.sessions_on(DAY)
        assert session.end_soc == 37.0

    def test_closed_sessions_survive_a_restart(self):
        sch = _schedule()
        _run_until_idle(sch, _at(6, 40), 20.0, step=1.0)
        restored = BatterySchedule.from_dict(sch.to_dict())
        assert restored.sessions_on(DAY) == sch.sessions_on(DAY)

    def test_store_without_sessions_restores_empty(self):
        data = _schedule().to_dict()
        del data["sessions"]
        restored = BatterySchedule.from_dict(data)
        assert restored.sessions_on(DAY) == []
        assert restored.t1_reserve is not None

    def test_unreadable_record_is_skipped(self):
        data = _schedule().to_dict()
        data["sessions"] = [{"slot": "NO_SUCH_SLOT"}]
        assert BatterySchedule.from_dict(data).sessions_on(DAY) == []

    def test_yesterdays_sessions_go_on_the_first_tick_of_a_new_day(self):
        sch = _schedule()
        _run_until_idle(sch, _at(6, 40), 20.0, step=1.0)
        sch.compute_operation(_at(0, 1, day=DAY + timedelta(days=1)), 50.0)
        assert sch.to_dict()["sessions"] == []

    def test_session_dict_round_trip(self):
        session = SlotSession(
            slot=SlotKind.DISCHARGE_MORNING_AUTO,
            started_at=_at(8, 37),
            start_soc=37.0,
            target_soc=22.0,
            ended_at=_at(8, 59),
            end_soc=22.0,
            reason=DisengageReason.TARGET_REACHED,
        )
        assert SlotSession.from_dict(session.to_dict()) == session


class TestUpcomingRuns:
    def test_night_shows_the_charge_then_the_discharge_from_its_target(self):
        sch = _schedule()
        charge, discharge = sch.upcoming_runs(_at(2), 20.0, is_workday=True)

        assert charge.slot is SlotKind.CHARGE_MORNING_AUTO
        assert (charge.start_soc, charge.target_soc) == (20.0, 37)
        assert charge.end == _at(7)
        expected = (
            SlotKind.CHARGE_MORNING_AUTO.profile.seconds_for_soc_traversal(20.0, 37.0)
            + SlotKind.CHARGE_MORNING_AUTO.profile.delayed_lead_sec
        )
        assert charge.start == _at(7) - timedelta(seconds=expected)

        assert discharge.slot is SlotKind.DISCHARGE_MORNING_AUTO
        assert discharge.start_soc == 37
        assert discharge.target_soc == 22
        assert (discharge.window_start, discharge.end) == (_at(8), _at(9))
        assert _at(8) < discharge.start < _at(9)

    def test_engaged_slot_is_not_a_run_but_its_target_carries_over(self):
        sch = _schedule()
        sch.compute_operation(_at(6, 40), 20.0, is_workday=True)
        [discharge] = sch.upcoming_runs(_at(6, 41), 21.0, is_workday=True)
        assert discharge.start_soc == 37

    def test_less_than_the_minimum_gap_is_no_run(self):
        # Charge window over; 23 -> 22 is below the discharge's 2 pp minimum.
        assert _schedule().upcoming_runs(_at(7, 30), 23.0, is_workday=True) == []

    def test_day_off_has_no_runs(self):
        sch = _schedule()
        sunday = DAY + timedelta(days=3)
        assert sch.upcoming_runs(_at(2, day=sunday), 20.0, is_workday=False) == []

    @pytest.mark.parametrize("hour", [7, 9])
    def test_window_past_drops_the_run(self, hour):
        sch = _schedule()
        runs = sch.upcoming_runs(_at(hour), 40.0, is_workday=True)
        assert SlotKind.CHARGE_MORNING_AUTO not in [r.slot for r in runs]
        if hour == 9:
            assert runs == []
