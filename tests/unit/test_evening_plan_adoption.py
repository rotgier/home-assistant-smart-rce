"""Who gets the last word over the evening slots — the planner or the user.

The planner writes its plan into the two evening slots, but stands down once
the user has edited them by hand that day. The single exception is a plan
emptied by tomorrow morning's prices: that is information the user did not
have when they edited, so it wins and hands control back.
"""

from datetime import date, datetime, time, timezone
from unittest.mock import MagicMock

from custom_components.smart_rce.application.battery_schedule_service import (
    BatteryScheduleService,
)
from custom_components.smart_rce.application.plan_application import PlanOutcome
from custom_components.smart_rce.domain.battery_schedule import (
    BatterySchedule,
    SetSlotEnabledCommand,
    SetSlotStartCommand,
    SlotKind,
)
from custom_components.smart_rce.domain.evening_plan import EveningPlan
from custom_components.smart_rce.domain.rce import RceDayPrices

TODAY = date(2026, 9, 21)  # Monday
NOW = datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc)
EARLY = SlotKind.DISCHARGE_EVENING_EARLY
LATE = SlotKind.DISCHARGE_EVENING_LATE

RICH = 700.0
POOR = 500.0


class _FakeRepo:
    def __init__(self, schedule: BatterySchedule) -> None:
        self.schedule = schedule

    async def persist(self) -> None:
        pass

    def save_if_changed(self) -> None:
        pass


def _service(schedule: BatterySchedule) -> BatteryScheduleService:
    return BatteryScheduleService(
        repo=_FakeRepo(schedule),
        clock=lambda: NOW,
        tasks=MagicMock(),
        notifier=MagicMock(),
    )


def _prices(**hours: float) -> RceDayPrices:
    prices = [POOR] * 24
    for hour, price in hours.items():
        prices[int(hour[1:])] = price
    return RceDayPrices(published_at=None, day=TODAY, hour_price=tuple(prices))


def _morning(price: float) -> RceDayPrices:
    prices = [price] * 24
    return RceDayPrices(
        published_at=None, day=date(2026, 9, 22), hour_price=tuple(prices)
    )


def _plan(today: RceDayPrices, tomorrow: RceDayPrices | None = None) -> EveningPlan:
    return EveningPlan.for_day(TODAY, today, is_workday=True, tomorrow=tomorrow)


async def test_a_plan_lands_in_the_slots_when_nobody_touched_them():
    schedule = BatterySchedule()
    service = _service(schedule)

    result = await service.adopt_evening_plan(_plan(_prices(h19=RICH, h20=RICH)), NOW)

    assert result.outcome is PlanOutcome.APPLIED
    entry = schedule.today_entry_for(EARLY)
    assert entry.enabled
    assert (entry.start, entry.end) == (time(19, 0), time(21, 0))


async def test_a_hand_edited_evening_is_left_alone():
    schedule = BatterySchedule()
    service = _service(schedule)
    await service.handle_slot_command(
        SetSlotStartCommand(scope="today", kind=EARLY, value=time(18, 0))
    )

    result = await service.adopt_evening_plan(_plan(_prices(h19=RICH, h20=RICH)), NOW)

    assert result.outcome is PlanOutcome.DEFERRED_TO_MANUAL
    assert schedule.today_entry_for(EARLY).start == time(18, 0)


async def test_a_plan_vetoed_by_the_morning_overrides_a_hand_edit():
    schedule = BatterySchedule()
    service = _service(schedule)
    await service.handle_slot_command(
        SetSlotEnabledCommand(scope="today", kind=EARLY, value=True)
    )

    plan = _plan(_prices(h19=RICH, h20=RICH), _morning(RICH + 100))

    assert plan.overruled_by_morning
    assert (await service.adopt_evening_plan(plan, NOW)).outcome is PlanOutcome.APPLIED
    assert not schedule.today_entry_for(EARLY).enabled


async def test_overriding_hands_the_evening_back_to_the_planner():
    schedule = BatterySchedule()
    service = _service(schedule)
    await service.handle_slot_command(
        SetSlotEnabledCommand(scope="today", kind=EARLY, value=True)
    )
    await service.adopt_evening_plan(
        _plan(_prices(h19=RICH, h20=RICH), _morning(RICH + 100)), NOW
    )

    assert not schedule.evening_is_hand_set_on(TODAY)


async def test_editing_a_non_evening_slot_does_not_lock_the_evening():
    schedule = BatterySchedule()
    service = _service(schedule)
    await service.handle_slot_command(
        SetSlotEnabledCommand(scope="today", kind=SlotKind.CHARGE_MORNING, value=True)
    )

    result = await service.adopt_evening_plan(_plan(_prices(h19=RICH, h20=RICH)), NOW)
    assert result.outcome is PlanOutcome.APPLIED


async def test_the_lock_expires_with_the_day():
    schedule = BatterySchedule()
    service = _service(schedule)
    await service.handle_slot_command(
        SetSlotEnabledCommand(scope="today", kind=EARLY, value=True)
    )

    # Same schedule, a day later — no clearing step ran, none was needed.
    assert schedule.evening_is_hand_set_on(TODAY)
    assert not schedule.evening_is_hand_set_on(date(2026, 9, 22))


async def test_an_empty_plan_switches_the_slots_off():
    schedule = BatterySchedule()
    service = _service(schedule)
    await service.adopt_evening_plan(_plan(_prices(h19=RICH, h20=RICH)), NOW)

    await service.adopt_evening_plan(_plan(_prices()), NOW)

    assert not schedule.today_entry_for(EARLY).enabled
    assert not schedule.today_entry_for(LATE).enabled


def test_the_lock_survives_a_store_round_trip():
    schedule = BatterySchedule()
    schedule.apply_manual_slot_command(
        SetSlotEnabledCommand(scope="today", kind=EARLY, value=True), today=TODAY
    )

    restored = BatterySchedule.from_dict(schedule.to_dict())

    assert restored.evening_is_hand_set_on(TODAY)


async def test_re_applying_the_same_plan_reports_no_change():
    # Distinct from standing down: the plan DID run, the slots already agreed.
    schedule = BatterySchedule()
    service = _service(schedule)
    plan = _plan(_prices(h19=RICH, h20=RICH))
    await service.adopt_evening_plan(plan, NOW)

    assert (
        await service.adopt_evening_plan(plan, NOW)
    ).outcome is PlanOutcome.ALREADY_CURRENT


async def test_the_button_overrides_a_hand_edit():
    # Pressing "recalculate" is an explicit request; deferring to the very
    # setting the user is asking to replace would make the button useless.
    schedule = BatterySchedule()
    service = _service(schedule)
    await service.handle_slot_command(
        SetSlotStartCommand(scope="today", kind=EARLY, value=time(18, 0))
    )

    result = await service.adopt_evening_plan(
        _plan(_prices(h19=RICH, h20=RICH)), NOW, force=True
    )

    assert result.outcome is PlanOutcome.APPLIED
    assert schedule.today_entry_for(EARLY).start == time(19, 0)
    assert not schedule.evening_is_hand_set_on(TODAY)


async def test_the_report_names_what_moved():
    schedule = BatterySchedule()
    service = _service(schedule)

    result = await service.adopt_evening_plan(_plan(_prices(h19=RICH, h20=RICH)), NOW)

    assert [c.kind for c in result.changes] == [EARLY]
    change = result.changes[0]
    assert change.was_enabled
    assert (change.after.start, change.after.end) == (time(19, 0), time(21, 0))


async def test_no_diff_when_the_slots_already_agree():
    schedule = BatterySchedule()
    service = _service(schedule)
    plan = _plan(_prices(h19=RICH, h20=RICH))
    await service.adopt_evening_plan(plan, NOW)

    result = await service.adopt_evening_plan(plan, NOW)

    assert not result.changed_anything
