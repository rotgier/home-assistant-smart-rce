"""The battery bands of the picked day: live today/tomorrow, past days rebuilt."""

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

from custom_components.smart_rce.application.battery_day_service import (
    BatteryDayService,
)
from custom_components.smart_rce.domain.battery_day import BatteryDay
from custom_components.smart_rce.domain.battery_schedule import (
    BatterySchedule,
    BatteryScheduleEntry,
    SlotBehavior,
    SlotKind,
)
from custom_components.smart_rce.domain.pv_charge import PvChargeRun
from custom_components.smart_rce.infrastructure import battery_day_history
from custom_components.smart_rce.infrastructure.battery_day_history import (
    BatteryDayHistory,
)

TZ = timezone(timedelta(hours=2))
SATURDAY = date(2026, 10, 17)
SUNDAY = date(2026, 10, 18)
H = 60
T2 = 16 * H


def _evening(kind: SlotKind, start: int, end: int) -> BatteryScheduleEntry:
    return BatteryScheduleEntry(
        kind=kind,
        enabled=True,
        start=time(start),
        end=time(end),
        target_soc=10.0,
        behavior=SlotBehavior.IMMEDIATE,
    )


def _schedule(*entries, evening_day=None) -> BatterySchedule:
    today = BatteryScheduleEntry.defaults_for_all_kinds()
    for entry in entries:
        today[entry.kind] = entry
    return BatterySchedule(today=today, evening_day=evening_day)


class TestEveningWindowsOn:
    def test_tonights_slots_run_today_and_repeat_tomorrow(self):
        sch = _schedule(
            _evening(SlotKind.DISCHARGE_EVENING_LATE, 20, 21),
            _evening(SlotKind.DISCHARGE_EVENING_EARLY, 18, 19),
            evening_day=SATURDAY,
        )
        today = sch.evening_windows_on(SATURDAY)
        assert [e.kind for e in today] == [
            SlotKind.DISCHARGE_EVENING_EARLY,
            SlotKind.DISCHARGE_EVENING_LATE,
        ]
        assert len(sch.evening_windows_on(SUNDAY)) == 2

    def test_slots_dated_to_tomorrow_do_not_run_today(self):
        sch = _schedule(
            _evening(SlotKind.DISCHARGE_EVENING_EARLY, 18, 20), evening_day=SUNDAY
        )
        assert sch.evening_windows_on(SATURDAY) == []
        assert len(sch.evening_windows_on(SUNDAY)) == 1


class TestReplay:
    """18.10.2026 (Sunday): gate open 09:00, 90% at 11:19, 100% at 11:47."""

    @staticmethod
    def _soc(minute: int) -> float:
        if minute < 9 * H:
            return 22.0
        if minute < 11 * H + 19:
            return 22.0 + (minute - 9 * H) * 0.48
        if minute < 11 * H + 47:
            return 90.0
        return 100.0

    def test_same_rule_as_live(self):
        run = PvChargeRun.replay(
            SUNDAY,
            gate_open=lambda m: m >= 9 * H,
            soc_at=self._soc,
            window_end=T2,
        )
        assert (run.start, run.plateau_at, run.full_at) == (540, 679, 707)
        assert run.end_soc == 100.0  # the level at T2, as the live run records it

    def test_grid_charge_slot_does_not_open_the_pv_run(self):
        # Gate open 06:20-06:55 by a charge slot (excluded by the caller), PV at 09:00.
        run = PvChargeRun.replay(
            SUNDAY,
            gate_open=lambda m: m >= 9 * H,
            soc_at=lambda m: 30.0,
            window_end=T2,
        )
        assert run.start == 9 * H
        assert run.end_soc == 30.0


class _State:
    def __init__(self, state, at, attributes=None):
        self.state = state
        self.last_changed = at
        self.attributes = attributes or {}


class _History(BatteryDayHistory):
    """Recorder replaced by a dict entity → states."""

    def __init__(self, states):
        self._states = states

    async def _history(self, day, entity_id, since, *, attributes=False):
        return self._states.get(entity_id, [])


def _at(day, h, m=0):
    return datetime.combine(day, time(h, m), tzinfo=TZ)


class TestHistoryAdapter:
    def test_rebuilds_sessions_and_pv_run(self, monkeypatch):
        monkeypatch.setattr(
            battery_day_history.dt_util, "as_local", lambda moment: moment
        )
        session = {
            "slot": "DISCHARGE_MORNING_AUTO",
            "start": _at(SATURDAY, 6).isoformat(),
            "start_soc": 40.0,
            "target": 22.0,
            "end": _at(SATURDAY, 7).isoformat(),
            "end_soc": 22.0,
            "reason": "target_reached",
        }
        old = dict(session, start=_at(SATURDAY - timedelta(days=1), 20).isoformat())
        states = {
            battery_day_history.SESSIONS_ENTITY: [
                _State("2", _at(SATURDAY, 22), {"sessions": [old, session]})
            ],
            battery_day_history.CHARGE_ALLOWED_ENTITY: [
                _State("on", _at(SATURDAY - timedelta(days=1), 23)),
                _State("off", _at(SATURDAY, 6)),
                _State("on", _at(SATURDAY, 6, 20)),
                _State("off", _at(SATURDAY, 6, 55)),
                _State("on", _at(SATURDAY, 11)),
            ],
            battery_day_history.ENGAGING_ENTITY: [
                _State("IDLE", _at(SATURDAY, 0)),
                _State("CHARGE_MORNING_AUTO", _at(SATURDAY, 6, 20)),
                _State("IDLE", _at(SATURDAY, 6, 55)),
            ],
            battery_day_history.SOC_ENTITY: [
                _State("22", _at(SATURDAY, 0)),
                _State("60", _at(SATURDAY, 12)),
                _State("90", _at(SATURDAY, 12, 56)),
                _State("100", _at(SATURDAY, 13, 31)),
            ],
        }
        day = asyncio.run(_History(states).async_day(SATURDAY))
        assert day.source == "recorder"
        assert [s.slot for s in day.sessions] == [SlotKind.DISCHARGE_MORNING_AUTO]
        assert day.pv is not None
        assert day.pv.start == 11 * H  # not the 06:20 grid charge
        assert (day.pv.plateau_at, day.pv.full_at) == (12 * H + 56, 13 * H + 31)
        assert all(b.done for b in day.pv.bands)

    def test_before_the_sessions_sensor_the_engaging_stretches_stand_in(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            battery_day_history.dt_util, "as_local", lambda moment: moment
        )
        states = {
            battery_day_history.ENGAGING_ENTITY: [
                _State("IDLE", _at(SATURDAY, 0)),
                _State("DISCHARGE_MORNING_AUTO", _at(SATURDAY, 8, 18)),
                _State("IDLE", _at(SATURDAY, 9)),
                _State("CHARGE_AFTERNOON", _at(SATURDAY, 14, 56)),
                _State("IDLE", _at(SATURDAY, 15, 50)),
            ],
            battery_day_history.SOC_ENTITY: [
                _State("60", _at(SATURDAY, 0)),
                _State("35", _at(SATURDAY, 9)),
                _State("80", _at(SATURDAY, 15, 50)),
            ],
        }
        day = asyncio.run(_History(states).async_day(SATURDAY))
        got = [
            (s.slot, s.started_at.time(), s.ended_at.time(), s.start_soc, s.end_soc)
            for s in day.sessions
        ]
        assert got == [
            (SlotKind.DISCHARGE_MORNING_AUTO, time(8, 18), time(9, 0), 60.0, 35.0),
            (SlotKind.CHARGE_AFTERNOON, time(14, 56), time(15, 50), 35.0, 80.0),
        ]
        assert all(s.target_soc is None and s.reason is None for s in day.sessions)

    def test_a_session_left_open_ends_where_its_slot_let_go(self, monkeypatch):
        # 19.10: the close at 20:22 changed only the attributes; had the last row
        # kept Late open, the chart would stretch it to now.
        monkeypatch.setattr(
            battery_day_history.dt_util, "as_local", lambda moment: moment
        )
        monkeypatch.setattr(
            battery_day_history.dt_util, "get_default_time_zone", lambda: TZ
        )
        late = {
            "slot": "DISCHARGE_EVENING_LATE",
            "start": _at(SATURDAY, 20).isoformat(),
            "start_soc": 40.0,
            "target": 10.0,
            "end": None,
            "end_soc": None,
            "reason": None,
        }
        states = {
            battery_day_history.SESSIONS_ENTITY: [
                _State("1", _at(SATURDAY, 20), {"sessions": [late]})
            ],
            battery_day_history.ENGAGING_ENTITY: [
                _State("IDLE", _at(SATURDAY, 0)),
                _State("IDLE", _at(SATURDAY, 20)),
                _State("DISCHARGE_EVENING_LATE", _at(SATURDAY, 20)),
                _State("IDLE", _at(SATURDAY, 20, 22)),
            ],
            battery_day_history.SOC_ENTITY: [
                _State("40", _at(SATURDAY, 20)),
                _State("10", _at(SATURDAY, 20, 22)),
            ],
        }
        day = asyncio.run(_History(states).async_day(SATURDAY))
        (session,) = day.sessions
        assert (session.ended_at, session.end_soc) == (_at(SATURDAY, 20, 22), 10.0)

    def test_sessions_read_attribute_only_changes(self, monkeypatch):
        calls = []

        def significant(*args, **kwargs):
            calls.append((args[3], kwargs))
            return {}

        class _Recorder:
            async def async_add_executor_job(self, job, *args):
                return job(*args)

        monkeypatch.setattr(
            battery_day_history, "get_instance", lambda hass: _Recorder()
        )
        monkeypatch.setattr(battery_day_history, "get_significant_states", significant)
        monkeypatch.setattr(
            battery_day_history, "state_changes_during_period", lambda *a: {}
        )
        asyncio.run(BatteryDayHistory(None).async_day(SATURDAY))
        assert calls == [
            ([battery_day_history.SESSIONS_ENTITY], {"significant_changes_only": False})
        ]

    def test_a_day_the_recorder_lost_is_empty(self):
        day = asyncio.run(_History({}).async_day(SATURDAY))
        assert (day.source, day.sessions, day.pv) == ("none", (), None)


class TestService:
    def _service(self, now):
        schedule = SimpleNamespace(
            sessions_today=["s"],
            upcoming_runs_on=lambda day: [f"run {day}"],
            evening_windows_on=lambda day: [],
        )
        pv = SimpleNamespace(outlook_on=lambda day: None)
        calls = []

        class _Hist:
            async def async_day(self, day):
                calls.append(day)
                return BatteryDay(day, "recorder")

        ems = SimpleNamespace(battery_schedule_service=schedule)
        return BatteryDayService(ems, pv, _Hist(), lambda: now), calls

    def test_today_and_tomorrow_are_live_past_is_loaded_once(self):
        service, calls = self._service(_at(SUNDAY, 12))
        today = asyncio.run(service.async_day(SUNDAY))
        assert (today.source, today.sessions) == ("live", ("s",))
        tomorrow = asyncio.run(service.async_day(SUNDAY + timedelta(days=1)))
        assert (tomorrow.source, tomorrow.sessions) == ("live", ())
        asyncio.run(service.async_day(SATURDAY))
        asyncio.run(service.async_day(SATURDAY))
        assert calls == [SATURDAY]
        assert (
            asyncio.run(service.async_day(SUNDAY + timedelta(days=5))).source == "none"
        )
