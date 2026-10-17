"""The evening slots are dated: tomorrow's plan written tonight stays still tonight."""

from datetime import date, datetime, time, timedelta, timezone

from custom_components.smart_rce.domain.battery_schedule import (
    BatterySchedule,
    BatteryScheduleEntry,
    MorningDischargePlan,
    SetSlotStartCommand,
    SlotBehavior,
    SlotEngaged,
    SlotKind,
)

TZ = timezone(timedelta(hours=2))
SATURDAY = date(2026, 10, 17)
SUNDAY = date(2026, 10, 18)
EARLY, MID, LATE = (
    SlotKind.DISCHARGE_EVENING_EARLY,
    SlotKind.DISCHARGE_EVENING_MID,
    SlotKind.DISCHARGE_EVENING_LATE,
)


def _at(day: date, h: int, m: int = 0) -> datetime:
    return datetime.combine(day, time(h, m), tzinfo=TZ)


def _evening(kind: SlotKind, start: int, end: int) -> BatteryScheduleEntry:
    return BatteryScheduleEntry(
        kind=kind,
        enabled=True,
        start=time(start),
        end=time(end),
        target_soc=10.0,
        behavior=SlotBehavior.IMMEDIATE,
    )


def _schedule(
    *entries: BatteryScheduleEntry, evening_day: date | None
) -> BatterySchedule:
    today = BatteryScheduleEntry.defaults_for_all_kinds()
    for entry in entries:
        today[entry.kind] = entry
    return BatterySchedule(today=today, evening_day=evening_day)


class TestTomorrowsSlotsStayStillTonight:
    def test_saturday_night_does_not_run_sundays_late_slot(self):
        # 21:05 Saturday: Sunday's plan has a 21-22 window, dated Sunday.
        sch = _schedule(_evening(LATE, 21, 22), evening_day=SUNDAY)
        _, events = sch.compute_operation(_at(SATURDAY, 21, 30), 80.0, is_workday=False)
        assert not [e for e in events if isinstance(e, SlotEngaged)]

    def test_sunday_runs_it(self):
        sch = _schedule(_evening(LATE, 21, 22), evening_day=SUNDAY)
        _, events = sch.compute_operation(_at(SUNDAY, 21, 30), 80.0, is_workday=False)
        assert [e.slot for e in events if isinstance(e, SlotEngaged)] == [LATE]

    def test_undated_slots_run_every_day(self):
        sch = _schedule(_evening(LATE, 21, 22), evening_day=None)
        _, events = sch.compute_operation(_at(SATURDAY, 21, 30), 80.0, is_workday=False)
        assert [e.slot for e in events if isinstance(e, SlotEngaged)] == [LATE]

    def test_a_past_date_still_runs_reused_slots(self):
        sch = _schedule(_evening(LATE, 21, 22), evening_day=SATURDAY)
        _, events = sch.compute_operation(_at(SUNDAY, 21, 30), 80.0, is_workday=False)
        assert [e.slot for e in events if isinstance(e, SlotEngaged)] == [LATE]


class TestEveningEnd:
    def test_latest_enabled_evening_slot_including_mid(self):
        sch = _schedule(
            _evening(EARLY, 18, 19), _evening(MID, 21, 22), evening_day=SATURDAY
        )
        assert sch.evening_end_on(SATURDAY) == time(22)

    def test_none_once_the_slots_are_tomorrows(self):
        sch = _schedule(_evening(LATE, 21, 22), evening_day=SUNDAY)
        assert sch.evening_end_on(SATURDAY) is None
        assert sch.evening_end_on(SUNDAY) == time(22)

    def test_none_without_an_enabled_evening_slot(self):
        assert _schedule(evening_day=None).evening_end_on(SATURDAY) is None


class TestStamp:
    def test_a_hand_edit_dates_the_evening_it_is_aimed_at(self):
        sch = _schedule(_evening(MID, 20, 21), evening_day=SATURDAY)
        cmd = SetSlotStartCommand(scope="today", kind=MID, value=time(19))
        assert sch.apply_manual_slot_command(cmd, today=SUNDAY)
        assert sch.evening_end_on(SATURDAY) is None
        assert sch.evening_is_hand_set_on(SUNDAY)

    def test_stamp_reports_a_change_once(self):
        sch = _schedule(evening_day=SATURDAY)
        assert sch.stamp_evening(SUNDAY)
        assert not sch.stamp_evening(SUNDAY)

    def test_survives_a_restart(self):
        sch = _schedule(_evening(LATE, 21, 22), evening_day=SUNDAY)
        restored = BatterySchedule.from_dict(sch.to_dict())
        assert restored.evening_end_on(SATURDAY) is None
        assert restored.evening_end_on(SUNDAY) == time(22)

    def test_a_store_without_it_restores_undated(self):
        data = _schedule(_evening(LATE, 21, 22), evening_day=SUNDAY).to_dict()
        del data["evening_day"]
        assert BatterySchedule.from_dict(data).evening_end_on(SATURDAY) == time(22)


class TestUpcomingTomorrow:
    def test_the_evening_preview_lists_tomorrows_morning_discharge(self):
        today = BatteryScheduleEntry.defaults_for_all_kinds()
        kind = SlotKind.DISCHARGE_MORNING_AUTO
        today[kind] = BatteryScheduleEntry.default_for(kind, enabled=True)
        sch = BatterySchedule(today=today)
        sch.set_morning_discharge(
            MorningDischargePlan.decide(
                day=SUNDAY, hour=8, gross_price=900.0, reserve=10, fill=10, day_off=True
            )
        )
        [run] = sch.upcoming_runs(
            _at(SATURDAY, 21, 30), 40.0, is_workday=False, day=SUNDAY
        )
        assert run.slot is kind
        assert run.end == _at(SUNDAY, 9)
        assert run.target_soc == 10
        # Not today: tomorrow's plan does not make a run on Saturday.
        assert sch.upcoming_runs(_at(SATURDAY, 21, 30), 40.0, is_workday=False) == []
