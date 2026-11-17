"""The afternoon run's morning veto only when tomorrow morning may sell.

16.11.2026: tomorrow 08:00 at 1536 gross outbid every T2 hour, so the veto
dropped the whole evening — but 17.11 was a dear workday run as FULL (night to
100%, no morning sale), so the energy kept only replaced a T3 night purchase
(user's decision 17.11.2026).
"""

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

from custom_components.smart_rce.application.ems import Ems
from custom_components.smart_rce.domain.dear_day_choice import DearDayVariant
from custom_components.smart_rce.domain.grid_export import GridExportManager
from custom_components.smart_rce.domain.input_state import InputState
from custom_components.smart_rce.domain.rce import RceDayPrices, RcePrices
import pytest

TODAY = date(2026, 11, 16)
TOMORROW = date(2026, 11, 17)
# Gross PLN/MWh as published (16.11 and 17.11.2026).
TODAY_GROSS = (
    642,
    609,
    563,
    596,
    574,
    663,
    823,
    857,
    760,
    678,
    641,
    606,
    628,
    857,
    1140,
    1580,
    1114,
    1035,
    963,
    959,
    856,
    727,
    772,
    732,
)
TOMORROW_GROSS = (
    1022,
    1032,
    1017,
    1013,
    993,
    843,
    1089,
    1468,
    1536,
    1490,
    1395,
    1355,
    1417,
    1840,
    2401,
    2434,
    2058,
    1685,
    1570,
    1485,
    1302,
    1043,
    957,
    813,
)


def _day(day: date, gross: tuple[int, ...]) -> RceDayPrices:
    return RceDayPrices(
        published_at=None, day=day, hour_price=tuple(g / 1.23 for g in gross)
    )


def _ems(
    *,
    choice: DearDayVariant | None,
    workday_tomorrow: bool = True,
    tomorrow_gross: tuple[int, ...] = TOMORROW_GROSS,
) -> Ems:
    schedule = MagicMock()
    schedule.export_threshold = 750.0
    schedule.dear_day_choice_for.return_value = (
        None if choice is None else MagicMock(chosen=choice)
    )
    ems = Ems(
        dod_policy=MagicMock(),
        grid_export=GridExportManager(),
        water_heater=MagicMock(),
        battery_schedule_service=schedule,
        battery_charge_service=MagicMock(),
        water_heater_reserved_service=MagicMock(),
        dod_repository=MagicMock(),
        dod_logger=MagicMock(),
        dod_actuator=MagicMock(),
        goodwe_ems_actuator=MagicMock(),
    )
    ems.rce_prices.rce_prices = RcePrices(
        fetched_at=datetime(2026, 11, 16, 14, 0, tzinfo=timezone.utc),
        today=_day(TODAY, TODAY_GROSS),
        tomorrow=_day(TOMORROW, tomorrow_gross),
    )
    ems.last_input_state = InputState(
        is_workday=True, is_workday_tomorrow=workday_tomorrow
    )
    return ems


def test_a_full_dear_morning_does_not_veto_the_evening_16_11_2026():
    plan = _ems(choice=DearDayVariant.FULL).build_evening_plan(for_tomorrow=False)

    assert plan is not None
    assert not plan.overruled_by_morning
    assert len(plan.windows) == 5


def test_a_dear_morning_run_as_normal_still_vetoes():
    plan = _ems(choice=DearDayVariant.NORMAL).build_evening_plan(for_tomorrow=False)

    assert plan is not None
    assert plan.overruled_by_morning
    assert plan.is_empty


def test_the_run_waits_while_the_dear_day_choice_is_not_made():
    assert _ems(choice=None).build_evening_plan(for_tomorrow=False) is None


@pytest.mark.parametrize(
    ("workday_tomorrow", "tomorrow_gross"),
    [
        (False, TOMORROW_GROSS),  # a day off: no dear-day choice at all
        (True, (*TOMORROW_GROSS[:12], 700, *TOMORROW_GROSS[13:])),  # 12:00 cheap
    ],
)
def test_otherwise_the_morning_vetoes_as_before(workday_tomorrow, tomorrow_gross):
    ems = _ems(
        choice=None, workday_tomorrow=workday_tomorrow, tomorrow_gross=tomorrow_gross
    )

    plan = ems.build_evening_plan(for_tomorrow=False)

    assert plan is not None
    assert plan.overruled_by_morning
