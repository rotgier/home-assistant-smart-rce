"""What DodPolicy holds the battery for: a planned sale, or tomorrow's T1 reserve."""

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import MagicMock

from custom_components.smart_rce.application.battery_schedule_service import (
    BatteryScheduleService,
)
from custom_components.smart_rce.domain.battery_schedule import (
    BatterySchedule,
    BatteryScheduleEntry,
    BatteryScheduleInput,
    MorningChargeFloor,
    MorningDischargePlan,
    ReserveWindow,
    SlotBehavior,
    SlotKind,
    T1SocReserve,
)
from custom_components.smart_rce.domain.evening_plan import EveningPlan
from custom_components.smart_rce.domain.rce import RceDayPrices

TZ = timezone(timedelta(hours=2))
SATURDAY = date(2026, 10, 17)
SUNDAY = date(2026, 10, 18)
MONDAY = date(2026, 10, 19)


class _Repo:
    def __init__(self, schedule: BatterySchedule) -> None:
        self.schedule = schedule

    async def persist(self) -> None:
        pass

    def save_if_changed(self) -> None:
        pass


def _service(schedule: BatterySchedule, now: datetime, soc: float = 60.0):
    service = BatteryScheduleService(
        repo=_Repo(schedule), clock=lambda: now, tasks=MagicMock(), notifier=None
    )
    service.update(BatteryScheduleInput(battery_soc=soc, is_workday=False))
    return service


def _at(day: date, h: int, m: int = 0) -> datetime:
    return datetime.combine(day, time(h, m), tzinfo=TZ)


def _plan(day: date, price: float, target: int = 10) -> MorningDischargePlan:
    return MorningDischargePlan.decide(
        day=day, hour=8, gross_price=price, reserve=target, fill=target, day_off=True
    )


def _with_evening(start: int, end: int, day: date) -> BatterySchedule:
    today = BatteryScheduleEntry.defaults_for_all_kinds()
    today[SlotKind.DISCHARGE_EVENING_EARLY] = BatteryScheduleEntry(
        kind=SlotKind.DISCHARGE_EVENING_EARLY,
        enabled=True,
        start=time(start),
        end=time(end),
        target_soc=10.0,
        behavior=SlotBehavior.IMMEDIATE,
    )
    return BatterySchedule(today=today, evening_day=day)


class TestSalePlanned:
    def test_an_evening_slot_ahead_holds_the_afternoon(self):
        sch = _with_evening(18, 20, SATURDAY)
        assert _service(sch, _at(SATURDAY, 14)).sale_planned_ahead(_at(SATURDAY, 14))

    def test_a_spent_evening_slot_does_not(self):
        sch = _with_evening(18, 20, SATURDAY)
        assert not _service(sch, _at(SATURDAY, 21)).sale_planned_ahead(
            _at(SATURDAY, 21)
        )

    def test_tonights_slot_does_not_hold_the_night_before(self):
        # 02:00: the evening is 16 hours away, the morning decides.
        sch = _with_evening(18, 20, SATURDAY)
        assert not _service(sch, _at(SATURDAY, 2)).sale_planned_ahead(_at(SATURDAY, 2))

    def test_tomorrow_morning_at_the_bar_holds(self):
        sch = BatterySchedule()
        sch.set_morning_discharge(_plan(SUNDAY, 750.0))
        assert _service(sch, _at(SATURDAY, 22)).sale_planned_ahead(_at(SATURDAY, 22))

    def test_tomorrow_morning_below_the_bar_does_not(self):
        sch = BatterySchedule()
        sch.set_morning_discharge(_plan(SUNDAY, 702.0))
        assert not _service(sch, _at(SATURDAY, 22)).sale_planned_ahead(
            _at(SATURDAY, 22)
        )

    def test_after_midnight_the_morning_ahead_is_todays(self):
        sch = BatterySchedule()
        sch.set_morning_discharge(_plan(SUNDAY, 900.0))
        assert _service(sch, _at(SUNDAY, 3)).sale_planned_ahead(_at(SUNDAY, 3))

    def test_nothing_to_sell_above_the_target_does_not_hold(self):
        sch = BatterySchedule()
        sch.set_morning_discharge(_plan(SUNDAY, 900.0, target=40))
        assert not _service(sch, _at(SATURDAY, 22), soc=35.0).sale_planned_ahead(
            _at(SATURDAY, 22)
        )

    def test_the_bar_is_the_users(self):
        sch = BatterySchedule(export_threshold=650.0)
        sch.set_morning_discharge(_plan(SUNDAY, 702.0))
        assert _service(sch, _at(SATURDAY, 22)).sale_planned_ahead(_at(SATURDAY, 22))


class TestReserveFloor:
    def test_the_reserve_of_the_morning_ahead(self):
        sch = BatterySchedule()
        reserve = T1SocReserve(
            day=MONDAY,
            value=38,
            flat=38,
            prev_without_worst=None,
            dip_kwh=None,
            source="tomorrow_live_soft",
            window=ReserveWindow.FULL,
        )
        sch.set_t1_reserve(reserve, _at(SUNDAY, 16))
        service = _service(sch, _at(SUNDAY, 20))
        assert service.reserve_floor(_at(SUNDAY, 20)) == 38
        assert service.reserve_floor(_at(MONDAY, 3)) == 38
        assert service.reserve_floor(_at(SATURDAY, 20)) is None

    def test_a_lifted_night_charge_is_not_for_the_house(self):
        # 29.10.2026: dear day, the night charged to 100 by 06:53, and the
        # house was let down to the 50% reserve before 07:00.
        sch = BatterySchedule(today=self._night_charge(enabled=True))
        sch.set_t1_reserve(self._reserve(), _at(SUNDAY, 16))
        sch.set_morning_charge_floor(MONDAY, self._floor(target=100))
        service = _service(sch, _at(MONDAY, 6, 55))
        assert service.reserve_floor(_at(MONDAY, 6, 55)) == 100

    def test_a_floor_below_the_reserve_leaves_the_reserve(self):
        sch = BatterySchedule(today=self._night_charge(enabled=True))
        sch.set_t1_reserve(self._reserve(), _at(SUNDAY, 16))
        sch.set_morning_charge_floor(MONDAY, self._floor(target=None))
        assert _service(sch, _at(SUNDAY, 22)).reserve_floor(_at(SUNDAY, 22)) == 38

    def test_no_lift_when_the_night_charge_is_off(self):
        sch = BatterySchedule(today=self._night_charge(enabled=False))
        sch.set_t1_reserve(self._reserve(), _at(SUNDAY, 16))
        sch.set_morning_charge_floor(MONDAY, self._floor(target=100))
        assert _service(sch, _at(SUNDAY, 22)).reserve_floor(_at(SUNDAY, 22)) == 38

    @staticmethod
    def _night_charge(*, enabled: bool) -> dict[SlotKind, BatteryScheduleEntry]:
        today = BatteryScheduleEntry.defaults_for_all_kinds()
        auto = today[SlotKind.CHARGE_MORNING_AUTO]
        today[SlotKind.CHARGE_MORNING_AUTO] = auto.with_enabled(enabled)
        return today

    @staticmethod
    def _reserve() -> T1SocReserve:
        return T1SocReserve(
            day=MONDAY,
            value=38,
            flat=38,
            prev_without_worst=None,
            dip_kwh=None,
            source="tomorrow_live_soft",
            window=ReserveWindow.FULL,
        )

    @staticmethod
    def _floor(target: int | None) -> MorningChargeFloor:
        return MorningChargeFloor(
            day=MONDAY,
            surplus_kwh=0.0,
            fill=100 if target else 10,
            dip=28,
            target=target,
            dear_day=target == 100,
        )


class TestExportThreshold:
    def test_defaults_to_750_and_survives_a_restart(self):
        sch = BatterySchedule()
        assert sch.export_threshold == 750.0
        assert sch.set_export_threshold(800.0)
        assert not sch.set_export_threshold(800.0)
        assert BatterySchedule.from_dict(sch.to_dict()).export_threshold == 800.0

    def test_evening_plan_follows_the_users_bar(self):
        prices = [500.0] * 24
        prices[19] = 720.0
        day_prices = RceDayPrices(None, SATURDAY, tuple(p / 1.23 for p in prices))
        assert EveningPlan.for_day(SATURDAY, day_prices, is_workday=False).is_empty
        plan = EveningPlan.for_day(
            SATURDAY, day_prices, is_workday=False, export_threshold=700.0
        )
        assert not plan.is_empty

    def test_morning_plan_follows_the_users_bar(self):
        common = {"day": SUNDAY, "hour": 8, "reserve": 10, "fill": 40, "day_off": True}
        assert MorningDischargePlan.decide(gross_price=720.0, **common).target == 40
        low_bar = MorningDischargePlan.decide(
            gross_price=720.0, export_threshold=700.0, **common
        )
        assert low_bar.target == 10


class TestMorningPlansByDay:
    def test_today_and_tomorrow_side_by_side(self):
        sch = BatterySchedule()
        sch.set_morning_discharge(_plan(SATURDAY, 800.0))
        sch.set_morning_discharge(_plan(SUNDAY, 900.0))
        assert sch.morning_discharge_for(SATURDAY).gross_price == 800.0
        assert sch.morning_discharge_for(SUNDAY).gross_price == 900.0

    def test_none_clears_a_day(self):
        sch = BatterySchedule()
        sch.set_morning_discharge(_plan(SUNDAY, 900.0))
        assert sch.set_morning_discharge(None, day=SUNDAY)
        assert sch.morning_discharge_for(SUNDAY) is None
        assert not sch.set_morning_discharge(None, day=SUNDAY)

    def test_old_days_are_dropped(self):
        sch = BatterySchedule()
        sch.set_morning_discharge(_plan(SATURDAY - timedelta(days=3), 800.0))
        sch.set_morning_discharge(_plan(SUNDAY, 900.0))
        assert sch.morning_discharge_for(SATURDAY - timedelta(days=3)) is None
